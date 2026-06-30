#!/usr/bin/env python3
"""
skyspice2klayout_all_devices.py

Hybrid SKY130 SPICE/CDL -> KLayout GDS placer with Magic guard rings, analog placement, and optional MOS unit-array synthesis.

Designed for your Linux Mint + KLayout 0.30.9 + Python 3.12 setup:
  * does NOT rely on registered SKY130 PCells
  * uses fixed-device GDS recursively when exact cells exist
  * uses libs.ref combined GDS for standard cells and IO cells
  * uses direct SKY130 draw backends for generated MOS/cap/diode/resistor devices
  * avoids the gdsfactory/kfactory private _get_default_kcl problem by patching
    the draw modules at runtime

This is a placement/import helper, not an autorouter. It preserves imported device
GDS hierarchy and writes net labels/metadata, but it does not create metal routing.
"""

import os
import re
import sys
import glob
import json
import time
import math
import ast
import random
import types
import shutil
import subprocess
import tempfile
import csv
import zlib
import traceback
import inspect
import importlib
import difflib

# IMPORTANT: insert external gdsfactory site-packages before importing pya/draw modules.
# Your working Mint setup used KLAYOUT_PY_SITE=$HOME/klayout_gf7/lib/python3.12/site-packages.
_extra_py_site = os.environ.get("KLAYOUT_PY_SITE")
if _extra_py_site and os.path.isdir(_extra_py_site) and _extra_py_site not in sys.path:
    sys.path.insert(0, _extra_py_site)

try:
    import pya
except Exception as e:
    raise SystemExit(
        "This script must be run by KLayout Python, e.g.\n"
        "  KLAYOUT_PATH=/usr/local/share/pdk/sky130A/libs.tech/klayout \\\n"
        "  klayout -b -r skyspice2klayout_all_devices.py -- my_netlist.spice\n\n"
        "Original import error: %s" % e
    )

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

PDK_ROOT = os.environ.get("PDK_ROOT", "/usr/local/share/pdk")
PDK = os.environ.get("PDK", "sky130A")
PDKPATH = os.environ.get("PDKPATH", os.path.join(PDK_ROOT, PDK))

SKY130_KLAYOUT_ROOTS = [
    os.path.join(PDKPATH, "libs.tech", "klayout", "python"),
    os.path.join(PDKPATH, "libs.tech", "klayout", "pymacros"),
]

CELLS_PY_ROOT = None
for _root in SKY130_KLAYOUT_ROOTS:
    if os.path.isdir(os.path.join(_root, "cells")):
        CELLS_PY_ROOT = _root
        break

if CELLS_PY_ROOT and CELLS_PY_ROOT not in sys.path:
    sys.path.insert(0, CELLS_PY_ROOT)

FIXED_DEVICES_DIR = os.environ.get(
    "SKY_FIXED_DEVICES_DIR",
    os.path.join(PDKPATH, "libs.tech", "klayout", "python", "cells", "fixed_devices"),
)
LIBS_REF_DIR = os.environ.get("SKY_LIBS_REF_DIR", os.path.join(PDKPATH, "libs.ref"))

# KLayout database unit: 0.001 um is normal enough for SKY130 scripts here.
LAYOUT_DBU = float(os.environ.get("SKY_LAYOUT_DBU", "0.001"))

TOP_NAME = os.environ.get("SKY_TOP", "TOP_AUTOPLACED_FROM_SPICE")
OUT_GDS = os.environ.get("SKY_OUT")
NETLIST = os.environ.get("SKY_NETLIST") or os.environ.get("NETLIST")

ADD_LABELS = os.environ.get("SKY_ADD_LABELS", "1") == "1"
ADD_RAILS = os.environ.get("SKY_ADD_RAILS", "1") == "1"
OVERWRITE = os.environ.get("SKY_OVERWRITE", "0") == "1"
OPEN_AFTER = os.environ.get("SKY_OPEN_AFTER", "0") == "1"

MAX_COLS = int(os.environ.get("SKY_MAX_COLS", "6"))
X_SPACING_UM = float(os.environ.get("SKY_X_SPACING_UM", "38"))
Y_SPACING_UM = float(os.environ.get("SKY_Y_SPACING_UM", "32"))
PAIR_Y_GAP_UM = float(os.environ.get("SKY_PAIR_Y_GAP_UM", "20"))
OTHER_Y_GAP_UM = float(os.environ.get("SKY_OTHER_Y_GAP_UM", "36"))

# If a device has m>1, the wrapper copies the generated/imported cell in X.
M_SPACING_UM = float(os.environ.get("SKY_M_SPACING_UM", "3"))

TEXT_LAYER = tuple(map(int, os.environ.get("SKY_TEXT_LAYER", "255,0").split(",")))
PIN_TEXT_LAYER = tuple(map(int, os.environ.get("SKY_PIN_TEXT_LAYER", "255,5").split(",")))
M1_RAIL_LAYER = tuple(map(int, os.environ.get("SKY_M1_RAIL_LAYER", "68,20").split(",")))
M1_TEXT_LAYER = tuple(map(int, os.environ.get("SKY_M1_TEXT_LAYER", "68,5").split(",")))

# Layout usability / annotation features.
# These are on non-critical/text/debug layers by default so they should not create device DRC.
PIN_LABELS = os.environ.get("SKY_PIN_LABELS", "1") == "1"
DEVICE_MAP_CARD = os.environ.get("SKY_DEVICE_MAP_CARD", "1") == "1"
SET_INSTANCE_PROPERTIES = os.environ.get("SKY_SET_INSTANCE_PROPERTIES", "1") == "1"
WRITE_DEVICE_MAP_FILES = os.environ.get("SKY_WRITE_DEVICE_MAP_FILES", "1") == "1"

# Route-guide display controls.  Backward compatible with SKY_NET_GUIDES=0/1,
# but the preferred knob is SKY_ROUTE_GUIDES/SKY_GUIDE_MODE:
#   off|none|0      -> no route guides
#   important|1     -> only top/high-fanout nets, power excluded by default
#   selected        -> only nets in SKY_GUIDE_NETS
#   all             -> all non-excluded nets
_GUIDE_MODE_ENV = os.environ.get("SKY_GUIDE_MODE", os.environ.get("SKY_ROUTE_GUIDES", os.environ.get("SKY_NET_GUIDES", "important")))
GUIDE_MODE = str(_GUIDE_MODE_ENV).strip().lower()
NET_GUIDES = GUIDE_MODE not in ("0", "false", "no", "off", "none", "disable", "disabled")
if GUIDE_MODE in ("1", "true", "yes", "on", "enable", "enabled"):
    GUIDE_MODE = "important"

PLACE_MODE = os.environ.get("SKY_PLACE_MODE", "compact").strip().lower()  # safe|compact|ultra
COMPACT_X_MARGIN_UM = float(os.environ.get("SKY_COMPACT_X_MARGIN_UM", "8"))
COMPACT_Y_MARGIN_UM = float(os.environ.get("SKY_COMPACT_Y_MARGIN_UM", "10"))
ULTRA_X_MARGIN_UM = float(os.environ.get("SKY_ULTRA_X_MARGIN_UM", "4"))
ULTRA_Y_MARGIN_UM = float(os.environ.get("SKY_ULTRA_Y_MARGIN_UM", "6"))
PIN_LABEL_MAG = float(os.environ.get("SKY_PIN_LABEL_MAG", "0.55"))
DEVICE_LABEL_MAG = float(os.environ.get("SKY_DEVICE_LABEL_MAG", "0.65"))
DEVICE_MAP_MAG = float(os.environ.get("SKY_DEVICE_MAP_MAG", "0.70"))
MAP_OFFSET_UM = float(os.environ.get("SKY_MAP_OFFSET_UM", "60"))
MAP_SIDE = os.environ.get("SKY_MAP_SIDE", "right").strip().lower()  # right|below
GUIDE_WIDTH_UM = float(os.environ.get("SKY_GUIDE_WIDTH_UM", "0.05"))
GUIDE_LAYER = tuple(map(int, os.environ.get("SKY_GUIDE_LAYER", "255,10").split(",")))
GUIDE_TEXT_LAYER = tuple(map(int, os.environ.get("SKY_GUIDE_TEXT_LAYER", "255,11").split(",")))
GUIDE_PER_NET_LAYERS = os.environ.get("SKY_GUIDE_PER_NET_LAYERS", "1") == "1"
GUIDE_LAYER_SPAN = max(1, int(os.environ.get("SKY_GUIDE_LAYER_SPAN", "16")))
GUIDE_NET_LAYER_MAP_RAW = os.environ.get("SKY_GUIDE_NET_LAYER_MAP", "")
GUIDE_NETS_RAW = os.environ.get("SKY_GUIDE_NETS", "")
GUIDE_EXCLUDE_NETS_RAW = os.environ.get(
    "SKY_GUIDE_EXCLUDE_NETS",
    "vdd,vdda,vccd1,vpwr,vss,vssa,vssd1,vgnd,gnd,0"
)
GUIDE_INCLUDE_POWER = os.environ.get("SKY_GUIDE_INCLUDE_POWER", "0") == "1"
GUIDE_TOP_N = int(os.environ.get("SKY_GUIDE_TOP_N", "12"))
GUIDE_MIN_PINS = int(os.environ.get("SKY_GUIDE_MIN_PINS", "2"))
ROUTING_PRIORITY_REPORT = os.environ.get("SKY_ROUTING_PRIORITY_REPORT", "1") == "1"
ROUTING_PRIORITY_CARD = os.environ.get("SKY_ROUTING_PRIORITY_CARD", "0") == "1"
MATCH_GROUPS = os.environ.get("SKY_MATCH_GROUPS", "1") == "1"
MATCH_GROUP_CARD = os.environ.get("SKY_MATCH_GROUP_CARD", "0") == "1"
RAIL_CLEARANCE_UM = float(os.environ.get("SKY_RAIL_CLEARANCE_UM", "12"))
MAGIC_DRC_CHECK = os.environ.get("SKY_MAGIC_DRC_CHECK", "0") == "1"

# Analog/digital layout-intelligence features.  These do not change the SPICE
# netlist or routed metal; they add reports, warnings, grouping, and smarter
# layout-review metadata for analog/digital layout work.
ANALOGSENSE = os.environ.get("SKY_ANALOGSENSE", "1") == "1"
TOPOLOGY_ANALYSIS = os.environ.get("SKY_TOPOLOGY_ANALYSIS", "1") == "1"
LAYOUT_WARNINGS = os.environ.get("SKY_LAYOUT_WARNINGS", "1") == "1"
ANALOGSENSE_CARD = os.environ.get("SKY_ANALOGSENSE_CARD", "0") == "1"
LAYOUT_WARNING_CARD = os.environ.get("SKY_LAYOUT_WARNING_CARD", "0") == "1"
WRITE_ANALOGSENSE_FILES = os.environ.get("SKY_WRITE_ANALOGSENSE_FILES", "1") == "1"
# SKY_LAYOUT_STYLE is intentionally separate from SKY_PLACE_MODE. PLACE_MODE
# controls spacing tightness; LAYOUT_STYLE controls engineering intent.
LAYOUT_STYLE = os.environ.get("SKY_LAYOUT_STYLE", os.environ.get("SKY_ANALOG_STYLE", "analog")).strip().lower()  # analog|digital|mixed|none
LARGE_MOS_W_UM = float(os.environ.get("SKY_LARGE_MOS_W_UM", "20"))
SENSITIVE_HPWL_UM = float(os.environ.get("SKY_SENSITIVE_HPWL_UM", "80"))
WARNING_CARD_TOP_N = int(os.environ.get("SKY_WARNING_CARD_TOP_N", "14"))
ANALOG_CARD_TOP_N = int(os.environ.get("SKY_ANALOG_CARD_TOP_N", "16"))
SUMMARY_CARD = os.environ.get("SKY_SUMMARY_CARD", "0") == "1"
ANALOG_STACK_PLACER = os.environ.get("SKY_ANALOG_STACK_PLACER", "1") == "1"
ANALOG_ROW_GAP_UM = float(os.environ.get("SKY_ANALOG_ROW_GAP_UM", "18"))
ANALOG_COL_GAP_UM = float(os.environ.get("SKY_ANALOG_COL_GAP_UM", "14"))
ANALOG_STACK_GAP_UM = float(os.environ.get("SKY_ANALOG_STACK_GAP_UM", "6"))


# Major analog-layout synthesis feature: unit-array / finger-array planning.
# This is the first version that can physically replace large MOS devices by
# an array of smaller equivalent unit devices.  It is intentionally optional
# through SKY_UNIT_ARRAY_MODE, because LVS may extract the array as multiple
# devices even though it is electrically equivalent to the original SPICE W.
#   SKY_UNIT_ARRAY_MODE=off     -> no unit arrays
#   SKY_UNIT_ARRAY_MODE=plan    -> write plans/reports only
#   SKY_UNIT_ARRAY_MODE=layout  -> replace large MOS cells by unit-array wrappers
UNIT_ARRAY_MODE = os.environ.get("SKY_UNIT_ARRAY_MODE", "layout").strip().lower()  # off|plan|layout
UNIT_ARRAY_MIN_W_UM = float(os.environ.get("SKY_UNIT_ARRAY_MIN_W_UM", "8"))
UNIT_W_UM = float(os.environ.get("SKY_UNIT_W_UM", "1"))
UNIT_ARRAY_MAX_UNITS = int(os.environ.get("SKY_UNIT_ARRAY_MAX_UNITS", "64"))
UNIT_ARRAY_ROWS = int(os.environ.get("SKY_UNIT_ARRAY_ROWS", "0"))  # 0 = auto
UNIT_ARRAY_GAP_UM = float(os.environ.get("SKY_UNIT_ARRAY_GAP_UM", "3.0"))
UNIT_ARRAY_ROW_GAP_UM = float(os.environ.get("SKY_UNIT_ARRAY_ROW_GAP_UM", "5.0"))
UNIT_ARRAY_USE_MAGIC_GR = os.environ.get("SKY_UNIT_ARRAY_USE_MAGIC_GR", "1") == "1"
UNIT_ARRAY_ADD_DUMMY_MARKERS = os.environ.get("SKY_UNIT_ARRAY_DUMMY_MARKERS", "1") == "1"
UNIT_ARRAY_DUMMY_LAYER = tuple(map(int, os.environ.get("SKY_UNIT_ARRAY_DUMMY_LAYER", "255,30").split(",")))
UNIT_ARRAY_TEXT_LAYER = tuple(map(int, os.environ.get("SKY_UNIT_ARRAY_TEXT_LAYER", "255,31").split(",")))
UNIT_ARRAY_WRITE_FILES = os.environ.get("SKY_UNIT_ARRAY_WRITE_FILES", "1") == "1"
UNIT_ARRAY_CARD = os.environ.get("SKY_UNIT_ARRAY_CARD", "0") == "1"

# Placement style selector.
#   analogpro   -> current topology-aware AnalogPro placement (default)
#   schematic   -> preserve SPICE/schematic order; large MOS devices are kept close in one top row
PLACEMENT_STYLE = os.environ.get("SKY_PLACEMENT_STYLE", os.environ.get("SKY_PLACER_STYLE", "analogpro")).strip().lower()
SCHEMATIC_LARGE_ROW = os.environ.get("SKY_SCHEMATIC_LARGE_ROW", "1").strip().lower() in ("1", "true", "yes", "on")
SCHEMATIC_LARGE_W_UM = float(os.environ.get("SKY_SCHEMATIC_LARGE_W_UM", str(UNIT_ARRAY_MIN_W_UM)))

# Optional separate ratline reference GDS outputs.
#   off    -> only clean main GDS
#   points -> clean main GDS + *_ratpoints.gds terminal markers
#   full   -> clean main GDS + *_ratlines_full.gds connected non-fab ratlines
#   both   -> both point-only and fully-connected reference GDS files
#   ask    -> ask interactively when possible
RATLINE_GDS_MODE = os.environ.get("SKY_RATLINE_GDS", os.environ.get("SKY_RATLINES", "off")).strip().lower()

# Magic backend guard-ring behavior.
# Default is selection-safe: only selected guard-ring FETs are generated by Magic.
# Set SKY_MAGIC_ALL_GUARD_RINGS=1 only when you intentionally want every MOS
# to be Magic-generated/guarded.
MAGIC_ALL_GUARD_RINGS = os.environ.get("SKY_MAGIC_ALL_GUARD_RINGS", "0").strip().lower() in ("1", "true", "yes", "on", "all")


# Movable-device annotation mode.
# Plain GDS cannot provide PCB-style live rubber-band ratsnest lines.  To make
# annotations follow when the user moves a device in KLayout, each generated
# device can be wrapped into a small annotated cell that contains:
#   device geometry + local pin labels + short local route stubs.
# Then moving the wrapper instance moves all local annotations with the device.
MOVABLE_DEVICE_WRAPPERS = os.environ.get("SKY_MOVABLE_DEVICE_WRAPPERS", "0") == "1"
LOCAL_PIN_STUBS = os.environ.get("SKY_LOCAL_PIN_STUBS", "0") == "1"
LOCAL_STUB_LAYER = tuple(map(int, os.environ.get("SKY_LOCAL_STUB_LAYER", "255,12").split(",")))
LOCAL_STUB_WIDTH_UM = float(os.environ.get("SKY_LOCAL_STUB_WIDTH_UM", "0.08"))
LOCAL_STUB_LEN_UM = float(os.environ.get("SKY_LOCAL_STUB_LEN_UM", "2.2"))
LOCAL_STUB_TEXT_AT_END = os.environ.get("SKY_LOCAL_STUB_TEXT_AT_END", "0") == "1"

# Top-level cross-device guide lines are static GDS shapes.  They are useful for
# a first routing plan, but they will not automatically follow if you drag cells
# in KLayout.  Default to movable local stubs only.  Set SKY_GUIDE_STYLE=top or
# both if you still want the old cross-device guide lines.
GUIDE_STYLE = os.environ.get("SKY_GUIDE_STYLE", "off").strip().lower()  # off|local|top|both
TOP_GUIDES_ENABLED = NET_GUIDES and GUIDE_STYLE in ("top", "both", "static", "global")
LOCAL_GUIDES_ENABLED = LOCAL_PIN_STUBS and GUIDE_STYLE not in ("off", "none", "0", "false")

# Interactive/fuzzy features. These are safe defaults: ask only when a terminal is available.
INTERACTIVE = os.environ.get("SKY_INTERACTIVE", "1") == "1"
ENABLE_FUZZY_MATCH = os.environ.get("SKY_FUZZY_MATCH", "1") == "1"
FUZZY_THRESHOLD = float(os.environ.get("SKY_FUZZY_THRESHOLD", "0.80"))
FUZZY_TOP_N = int(os.environ.get("SKY_FUZZY_TOP_N", "8"))

# Guard-ring behaviour for MOSFETs.
#   SKY_GUARD_RING=ask  -> ask user and let them select FET instances
#   SKY_GUARD_RING=yes  -> use SKY_GR_INSTS, default all if SKY_GR_INSTS is empty
#   SKY_GUARD_RING=no   -> no guard rings
#   SKY_GR_INSTS=all or X1,X2,XM3 or 1,3,5
SKY_GUARD_RING = os.environ.get("SKY_GUARD_RING", "ask").strip().lower()
SKY_GR_INSTS = os.environ.get("SKY_GR_INSTS", "").strip()
SKY_GRW_UM = float(os.environ.get("SKY_GRW_UM", "0.17"))

# Guard-ring backend.
#   magic  -> generate selected guarded MOS devices through Magic's SKY130 toolkit
#             (closest to Magic File -> Import SPICE behaviour)
#   direct -> old KLayout/gdsfactory draw_fet.py bulk=guard ring path
#   auto   -> try magic, then fall back to direct if Magic fails
SKY_GR_BACKEND = os.environ.get("SKY_GR_BACKEND", "magic").strip().lower()
SKY_MAGIC_BIN = os.environ.get("SKY_MAGIC_BIN", "magic").strip()
SKY_MAGIC_RC = os.environ.get(
    "SKY_MAGIC_RC",
    os.path.join(PDKPATH, "libs.tech", "magic", f"{PDK}.magicrc"),
)
SKY_GR_MAGIC_KEEP = os.environ.get("SKY_GR_MAGIC_KEEP", "0") == "1"
SKY_GR_MAGIC_FALLBACK_DIRECT = os.environ.get("SKY_GR_MAGIC_FALLBACK_DIRECT", "0") == "1"

# Optional non-interactive replacement map for missing/renamed devices. Examples:
#   SKY_DEVICE_MAP='sky130_fd_pr__nfet_018v_lvt=sky130_fd_pr__nfet_01v8_lvt'
#   SKY_DEVICE_MAP='{"old":"new"}'
SKY_DEVICE_MAP = os.environ.get("SKY_DEVICE_MAP", "").strip()

# Direct MOS models confirmed by the uploaded SKY130 draw_fet.py backend.
SUPPORTED_MOS_MODELS = {
    "sky130_fd_pr__pfet_01v8",
    "sky130_fd_pr__pfet_01v8_lvt",
    "sky130_fd_pr__pfet_01v8_hvt",
    "sky130_fd_pr__pfet_g5v0d10v5",
    "sky130_fd_pr__nfet_01v8",
    "sky130_fd_pr__nfet_01v8_lvt",
    "sky130_fd_pr__nfet_01v8_hvt",
    "sky130_fd_pr__nfet_03v3_nvt",
    "sky130_fd_pr__nfet_05v0_nvt",
    "sky130_fd_pr__nfet_g5v0d10v5",
}

# Other direct-draw primitive names used for fuzzy suggestions.
SUPPORTED_DIRECT_NAMES = set(SUPPORTED_MOS_MODELS) | {
    "sky130_fd_pr__cap_mim_m3_1",
    "sky130_fd_pr__cap_mim_m3_2",
    "sky130_fd_pr__model__cap_mim",
    "sky130_fd_pr__model__cap_mim_m4",
    "sky130_fd_pr__photodiode",
    "sky130_fd_pr__diode_pw2nd_05v5",
    "sky130_fd_pr__diode_pw2nd_05v5_lvt",
    "sky130_fd_pr__diode_pw2nd_05v5_nvt",
    "sky130_fd_pr__diode_pw2nd_11v0",
    "sky130_fd_pr__cap_var_lvt",
    "sky130_fd_pr__cap_var_hvt",
    "sky130_fd_pr__res_generic_l1",
    "sky130_fd_pr__res_generic_m1",
    "sky130_fd_pr__res_generic_m2",
    "sky130_fd_pr__res_generic_m3",
    "sky130_fd_pr__res_generic_m4",
    "sky130_fd_pr__res_generic_m5",
    "sky130_fd_pr__res_high_po_0p35",
    "sky130_fd_pr__res_high_po_0p69",
    "sky130_fd_pr__res_high_po_1p41",
    "sky130_fd_pr__res_high_po_2p85",
    "sky130_fd_pr__res_high_po_5p73",
    "sky130_fd_pr__res_xhigh_po_0p35",
    "sky130_fd_pr__res_xhigh_po_0p69",
    "sky130_fd_pr__res_xhigh_po_1p41",
    "sky130_fd_pr__res_xhigh_po_2p85",
    "sky130_fd_pr__res_xhigh_po_5p73",
}

KNOWN_LIBS = [
    "sky130_fd_sc_hdll",
    "sky130_fd_sc_hd",
    "sky130_fd_sc_hs",
    "sky130_fd_sc_ms",
    "sky130_fd_sc_ls",
    "sky130_fd_sc_lp",
    "sky130_fd_sc_hvl",
    "sky130_fd_io",
    "sky130_fd_pr",
    "sky130_ef_io",
    "sky130_ml_xx_hd",
]

_GF_UNIQUE_COUNTER = 0

UNIT_TO_UM = {
    "": 1.0,
    "u": 1.0,
    "um": 1.0,
    "µ": 1.0,
    "µm": 1.0,
    "n": 0.001,
    "nm": 0.001,
    "m": 1000.0,
    "mm": 1000.0,
    "p": 0.000001,
    "pm": 0.000001,
}

# -----------------------------------------------------------------------------
# Small utilities
# -----------------------------------------------------------------------------


def die(msg):
    raise RuntimeError(msg)


def dbu_um(layout, value_um):
    return int(round(float(value_um) / layout.dbu))


def clean_token(tok):
    return str(tok).strip().strip("'\"{}(),")


def clean_line(line):
    s = str(line).strip()
    if not s:
        return ""
    if s.startswith("*"):
        return ""
    # Ignore common inline comment styles. Do not strip '$' because xschem may use it.
    for marker in ["//", ";"]:
        if marker in s:
            s = s.split(marker, 1)[0].strip()
    return s


def sanitize_cell_name(name):
    s = re.sub(r"[^A-Za-z0-9_.$]", "_", str(name))
    if not s:
        s = "CELL"
    if s[0].isdigit():
        s = "C_" + s
    return s[:180]


def json_safe(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    try:
        if hasattr(obj, "name"):
            return str(obj.name)
    except Exception:
        pass
    return str(obj)


def _safe_eval_numeric_expr(expr, params=None):
    """Safely evaluate simple SPICE numeric expressions such as {S1*L}."""
    if expr is None:
        return None
    params = params or {}
    s = str(expr).strip().strip("{}'\"").replace("μ", "µ")
    if not s:
        return None

    # Direct number with optional unit first.
    m = re.match(r"^([+-]?\d+(?:\.\d*)?|\.\d+)(?:e([+-]?\d+))?([a-zA-Zµ]*)$", s.replace(" ", ""))
    if m:
        base = float(m.group(1))
        exp = int(m.group(2)) if m.group(2) else 0
        suffix = m.group(3).lower()
        return base * (10 ** exp) * UNIT_TO_UM.get(suffix, 1.0)

    # Replace known param names case-insensitively.
    env = {str(k).lower(): float(v) for k, v in params.items() if isinstance(v, (int, float))}

    allowed_nodes = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant, ast.Name,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Load,
    )
    try:
        tree = ast.parse(s, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, allowed_nodes):
                return None
            if isinstance(node, ast.Name) and node.id.lower() not in env:
                return None

        def ev(node):
            if isinstance(node, ast.Expression):
                return ev(node.body)
            if isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return float(node.value)
                raise ValueError("bad const")
            if isinstance(node, ast.Num):
                return float(node.n)
            if isinstance(node, ast.Name):
                return env[node.id.lower()]
            if isinstance(node, ast.UnaryOp):
                v = ev(node.operand)
                return -v if isinstance(node.op, ast.USub) else v
            if isinstance(node, ast.BinOp):
                a, b = ev(node.left), ev(node.right)
                if isinstance(node.op, ast.Add): return a + b
                if isinstance(node.op, ast.Sub): return a - b
                if isinstance(node.op, ast.Mult): return a * b
                if isinstance(node.op, ast.Div): return a / b
                if isinstance(node.op, ast.Pow): return a ** b
            raise ValueError("bad expr")
        return float(ev(tree))
    except Exception:
        return None


def parse_value_to_um(value, params=None):
    return _safe_eval_numeric_expr(value, params=params)


def parse_numeric(value, default=None):
    if value is None:
        return default
    try:
        return float(str(value).strip().strip("{}'\""))
    except Exception:
        v = parse_value_to_um(value)
        return default if v is None else v


def parse_int(value, default=1):
    if value is None:
        return default
    try:
        return int(float(str(value).strip().strip("{}'\"")))
    except Exception:
        return default


def parse_boolish(value, default=1):
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("0", "false", "no", "none", "off"):
        return 0
    if s in ("1", "true", "yes", "on"):
        return 1
    return default


def number_to_tag(value, digits=2):
    if value is None:
        return None
    return (f"{float(value):.{digits}f}").replace(".", "p")


def join_continuation_lines(path):
    result = []
    current = ""
    with open(path, "r", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.lstrip().startswith("+"):
                current += " " + line.lstrip()[1:].strip()
            else:
                if current:
                    result.append(current)
                current = line
    if current:
        result.append(current)
    return result


def parse_params(tokens):
    params = {}
    for token in tokens:
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        k = clean_token(k).lower()
        v = clean_token(v)
        if k:
            params[k] = v
    return params


def lib_for_cell(cell_name):
    cell_name = str(cell_name)
    for lib in sorted(KNOWN_LIBS, key=len, reverse=True):
        if cell_name.startswith(lib + "__") or cell_name == lib:
            return lib
    if "__" in cell_name:
        return cell_name.split("__", 1)[0]
    return None


def is_standard_cell(cell_name):
    lib = lib_for_cell(cell_name)
    return bool(lib and lib.startswith("sky130_fd_sc_"))


def is_io_cell(cell_name):
    lib = lib_for_cell(cell_name)
    return lib in ("sky130_fd_io", "sky130_ef_io") or str(cell_name).startswith("sky130_ef_io__")


def is_mos(cell_name):
    low = str(cell_name).lower()
    return "nfet" in low or "pfet" in low


def is_regular_pr_mos(cell_name):
    """Return True only for MOS models that the uploaded draw_fet.py backend supports exactly."""
    low = str(cell_name).lower()
    return low in SUPPORTED_MOS_MODELS


def is_pr_mos_like(cell_name):
    """MOS-looking primitive name, even if not supported exactly. Used for fuzzy repair."""
    low = str(cell_name).lower()
    return low.startswith("sky130_fd_pr__") and is_mos(low) and "rf_" not in low


def mos_kind(cell_name):
    low = str(cell_name).lower()
    if "pfet" in low:
        return "pfet"
    if "nfet" in low:
        return "nfet"
    return None


def is_cap(cell_name):
    return "cap" in str(cell_name).lower()


def is_diode(cell_name):
    low = str(cell_name).lower()
    return "diode" in low or "photodiode" in low


def is_bjt(cell_name):
    low = str(cell_name).lower()
    return "npn" in low or "pnp" in low or "bjt" in low


def is_res(cell_name):
    return "res_" in str(cell_name).lower()


def device_category(cell_name):
    low = str(cell_name).lower()
    if is_standard_cell(low):
        return "standard_cell"
    if is_io_cell(low):
        return "io_cell"
    if "rf_coil" in low or "rf_test_coil" in low:
        return "rf_coil"
    if is_mos(low):
        return "mos"
    if is_bjt(low):
        return "bjt"
    if is_diode(low):
        return "diode"
    if is_cap(low):
        return "cap"
    if is_res(low):
        return "res"
    return "unknown"

# -----------------------------------------------------------------------------
# Fuzzy device-name resolution and guard-ring selection
# -----------------------------------------------------------------------------

_LIBSREF_CELL_CACHE = {}


def parse_device_map_env():
    if not SKY_DEVICE_MAP:
        return {}
    try:
        obj = json.loads(SKY_DEVICE_MAP)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
    except Exception:
        pass
    out = {}
    for item in re.split(r"[,;]", SKY_DEVICE_MAP):
        if "=" in item:
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                out[k] = v
    return out


def clone_device_with_cell(device, new_cell, reason="manual_alt"):
    d = dict(device)
    d["original_cell"] = device.get("original_cell", device.get("cell"))
    d["cell"] = str(new_cell)
    d["lib"] = lib_for_cell(new_cell)
    d["category"] = device_category(new_cell)
    d["alternate_reason"] = reason
    return d


def normalize_for_match(name):
    s = str(name).lower()
    s = s.replace("sky130_fd_pr__", "pr__")
    s = s.replace("sky130_fd_sc_hd__", "sc_hd__")
    s = s.replace("sky130_fd_sc_hvl__", "sc_hvl__")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def libsref_gds_candidates_for_lib(lib):
    if not lib:
        return []
    gds_dir = os.path.join(LIBS_REF_DIR, lib, "gds")
    candidates = [
        os.path.join(gds_dir, f"{lib}.gds"),
        os.path.join(gds_dir, "sky130_ef_io.gds"),
        os.path.join(gds_dir, "sky130_fd_io.gds"),
        os.path.join(gds_dir, "sky130_fd_pr.gds"),
    ]
    candidates.extend(glob.glob(os.path.join(gds_dir, "**", "*.gds"), recursive=True))
    seen = set()
    out = []
    for c in candidates:
        if c not in seen and os.path.isfile(c):
            seen.add(c)
            out.append(c)
    return out


def list_cells_inside_gds(gds_path):
    if gds_path in _LIBSREF_CELL_CACHE:
        return _LIBSREF_CELL_CACHE[gds_path]
    names = []
    try:
        tmp = pya.Layout()
        tmp.read(gds_path)
        names = sorted({c.name for c in tmp.each_cell()})
    except Exception:
        names = []
    _LIBSREF_CELL_CACHE[gds_path] = names
    return names


def build_candidate_name_set(device, fixed_index):
    """Build a reasonably complete candidate universe for fuzzy matching."""
    candidates = set(SUPPORTED_DIRECT_NAMES)
    candidates.update(fixed_index.keys())

    lib = lib_for_cell(device.get("cell", ""))
    if lib:
        for gds in libsref_gds_candidates_for_lib(lib):
            candidates.update(list_cells_inside_gds(gds))

    if not lib or device_category(device.get("cell", "")) in ("unknown", "standard_cell", "io_cell"):
        for common_lib in ("sky130_fd_sc_hd", "sky130_fd_pr", "sky130_fd_io", "sky130_ef_io"):
            for gds in libsref_gds_candidates_for_lib(common_lib):
                candidates.update(list_cells_inside_gds(gds))

    return sorted(candidates)


def score_candidate(requested, candidate):
    a = normalize_for_match(requested)
    b = normalize_for_match(candidate)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    req_cat = device_category(requested)
    cand_cat = device_category(candidate)
    if req_cat == cand_cat and req_cat != "unknown":
        ratio += 0.05
    if mos_kind(requested) and mos_kind(requested) == mos_kind(candidate):
        ratio += 0.05
    if lib_for_cell(requested) and lib_for_cell(requested) == lib_for_cell(candidate):
        ratio += 0.05
    return min(ratio, 1.0)


def top_fuzzy_candidates(device, fixed_index, threshold=None, top_n=None):
    threshold = FUZZY_THRESHOLD if threshold is None else threshold
    top_n = FUZZY_TOP_N if top_n is None else top_n
    requested = device.get("cell", "")
    candidates = build_candidate_name_set(device, fixed_index)
    scored = []
    for cand in candidates:
        if cand == requested:
            continue
        score = score_candidate(requested, cand)
        if score >= threshold:
            scored.append((score, cand))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[:top_n]


def can_prompt():
    return INTERACTIVE


def input_or_default(prompt, default=""):
    try:
        return input(prompt)
    except EOFError:
        return default
    except KeyboardInterrupt:
        print("\nUser cancelled prompt; using default.")
        return default


def should_offer_fuzzy_alternate(device, fixed_index, fixed_lower_index):
    """Only ask for alternatives when this looks like a name/model mismatch, not a backend crash."""
    cell = str(device.get("cell", ""))
    low = cell.lower()
    if cell in fixed_index or low in fixed_lower_index:
        return False
    if low in SUPPORTED_DIRECT_NAMES:
        # This is a supported generator name. If it failed, the problem is not name matching.
        return False
    if is_pr_mos_like(low) and not is_regular_pr_mos(low):
        return True
    # Standard/IO typos and odd primitive names should get suggestions.
    return True


def prompt_for_alternate(device, fixed_index, resolution_map):
    if not ENABLE_FUZZY_MATCH or not can_prompt():
        return None

    original = device.get("cell", "")
    if original in resolution_map:
        return resolution_map[original]

    matches = top_fuzzy_candidates(device, fixed_index)
    if not matches:
        return None

    print("\n--- Device not matched exactly ---")
    print("Instance :", device.get("inst"))
    print("Requested:", original)
    print("Category :", device.get("category"))
    print("Nodes    :", ",".join(device.get("nodes", [])))
    print("Closest PDK/device names:")
    for i, (score, cand) in enumerate(matches, 1):
        print("  %2d) %.1f%%  %s" % (i, score * 100.0, cand))
    print("  0) skip this instance")
    print("You can also type an exact device/cell name manually.")

    ans = input_or_default("Select alternate [0-%d/name, default 0]: " % len(matches), "0").strip()
    if not ans or ans.lower() in ("0", "s", "skip", "n", "no"):
        return None

    selected = None
    if ans.isdigit():
        idx = int(ans)
        if 1 <= idx <= len(matches):
            selected = matches[idx - 1][1]
    else:
        selected = ans

    if not selected:
        return None

    apply_all = input_or_default("Use this alternate for all future occurrences of this requested name? [Y/n]: ", "y").strip().lower()
    if apply_all in ("", "y", "yes"):
        resolution_map[original] = selected
    return selected


def parse_instance_selection(selection, fet_devices):
    if not selection:
        return set()
    s = selection.strip()
    if s.lower() in ("all", "a", "*"):
        return {d["inst"].lower() for d in fet_devices}
    if s.lower() in ("none", "no", "n", "0", "skip"):
        return set()
    tokens = re.split(r"[ ,;]+", s)
    selected = set()
    by_inst = {d["inst"].lower(): d for d in fet_devices}
    for tok in tokens:
        if not tok:
            continue
        key = tok.lower()
        if key.isdigit():
            idx = int(key)
            if 1 <= idx <= len(fet_devices):
                selected.add(fet_devices[idx - 1]["inst"].lower())
        elif key in by_inst:
            selected.add(key)
    return selected


def choose_guard_ring_instances(devices):
    fet_devices = [d for d in devices if is_regular_pr_mos(d.get("cell", ""))]
    if not fet_devices:
        return set(), {"guard_rings_enabled": False, "reason": "no supported MOS FETs"}

    mode = SKY_GUARD_RING
    if mode in ("0", "false", "no", "none", "off"):
        return set(), {"guard_rings_enabled": False, "mode": mode}

    if mode in ("1", "true", "yes", "on", "all"):
        selection = SKY_GR_INSTS or "all"
        chosen = parse_instance_selection(selection, fet_devices)
        return chosen, {"guard_rings_enabled": bool(chosen), "mode": mode, "selection": selection, "selected": sorted(chosen)}

    if not can_prompt():
        return set(), {"guard_rings_enabled": False, "mode": "ask", "reason": "interactive input unavailable"}

    print("\nSupported MOSFETs found for optional guard rings:")
    for i, d in enumerate(fet_devices, 1):
        print("  %2d) %-12s %-32s W=%s L=%s nf=%s m=%s nodes=%s" % (
            i, d.get("inst"), d.get("cell"), d.get("w_um"), d.get("l_um"),
            d.get("nf"), d.get("m"), ",".join(d.get("nodes", []))
        ))
    ans = input_or_default("Add guard rings to selected FETs? [y/N]: ", "n").strip().lower()
    if ans not in ("y", "yes"):
        return set(), {"guard_rings_enabled": False, "mode": "ask", "answer": ans or "default_no"}

    selection = input_or_default("Type FET instance names/numbers, or 'all' [default all]: ", "all").strip() or "all"
    chosen = parse_instance_selection(selection, fet_devices)
    return chosen, {"guard_rings_enabled": bool(chosen), "mode": "ask", "selection": selection, "selected": sorted(chosen)}

# -----------------------------------------------------------------------------
# SPICE parsing
# -----------------------------------------------------------------------------


def extract_instance(line, param_context=None):
    param_context = param_context or {}
    s = clean_line(line)
    if not s or s.startswith("."):
        return None

    tokens = [clean_token(t) for t in s.split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return None

    inst_name = tokens[0]
    sky_tokens = []
    for i, t in enumerate(tokens):
        if re.match(r"^sky130_[A-Za-z0-9_]+__[A-Za-z0-9_]+$", t):
            sky_tokens.append((i, t))

    if not sky_tokens:
        return None

    model_index, cell_name = sky_tokens[-1]
    nodes = tokens[1:model_index]
    params = parse_params(tokens[model_index + 1:])

    nf_raw = params.get("nf", params.get("fingers", "1"))
    m_raw = params.get("m", params.get("mult", "1"))

    w_um = parse_value_to_um(params.get("w") or params.get("width") or params.get("wid"), param_context)
    l_um = parse_value_to_um(params.get("l") or params.get("length") or params.get("len"), param_context)

    return {
        "inst": inst_name,
        "cell": cell_name,
        "lib": lib_for_cell(cell_name),
        "category": device_category(cell_name),
        "nodes": nodes,
        "params_raw": params,
        "w_um": w_um,
        "l_um": l_um,
        "nf": parse_int(nf_raw, 1),
        "m": parse_int(m_raw, 1),
        "raw": s,
    }


def parse_netlist(path):
    devices = []
    metadata = {
        "subckts": [],
        "globals": [],
        "voltage_sources": [],
        "control_blocks_present": False,
        "params": {},
    }
    param_values = metadata["params"]

    for line in join_continuation_lines(path):
        parse_param_assignment(line, param_values)
        s = clean_line(line)
        if not s:
            continue
        low = s.lower()
        toks = s.split()

        if low.startswith(".subckt"):
            metadata["subckts"].append(s)
        elif low.startswith(".global"):
            metadata["globals"].extend(toks[1:])
        elif low.startswith(".control"):
            metadata["control_blocks_present"] = True
        elif toks and toks[0].upper().startswith("V") and len(toks) >= 4:
            metadata["voltage_sources"].append({
                "name": toks[0],
                "positive": toks[1],
                "negative": toks[2],
                "value": " ".join(toks[3:]),
            })

        item = extract_instance(s, param_values)
        if item:
            devices.append(item)

    return devices, metadata



def parse_param_assignment(line, param_values):
    """Update param_values from simple .param lines. Handles: .param S1 = 64 and .param L=1."""
    s = clean_line(line)
    if not s.lower().startswith(".param"):
        return
    body = s[6:].strip()
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", body)
    if not m:
        return
    key = m.group(1)
    val = m.group(2).strip().split()[0]
    num = parse_value_to_um(val, param_values)
    if num is not None:
        param_values[key.lower()] = num

# -----------------------------------------------------------------------------
# GDS indexing and importing
# -----------------------------------------------------------------------------


def build_gds_index(root_dir):
    index = {}
    lower_index = {}
    if not root_dir or not os.path.isdir(root_dir):
        return index, lower_index
    for path in glob.glob(os.path.join(root_dir, "**", "*.gds"), recursive=True):
        stem = os.path.splitext(os.path.basename(path))[0]
        index.setdefault(stem, []).append(path)
        lower_index.setdefault(stem.lower(), []).append(path)
    return index, lower_index


def find_libsref_gds_for_cell(cell_name):
    lib = lib_for_cell(cell_name)
    if not lib:
        return None

    # Standard-cell libraries are normally one combined GDS containing many cells.
    gds_dir = os.path.join(LIBS_REF_DIR, lib, "gds")
    candidates = [
        os.path.join(gds_dir, f"{lib}.gds"),
        os.path.join(gds_dir, f"{cell_name}.gds"),
    ]

    # IO cells sometimes have exact per-cell files and also combined files.
    if is_io_cell(cell_name):
        candidates = [
            os.path.join(gds_dir, f"{cell_name}.gds"),
            os.path.join(gds_dir, f"{lib}.gds"),
            os.path.join(gds_dir, "sky130_ef_io.gds"),
            os.path.join(gds_dir, "sky130_fd_io.gds"),
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    matches = glob.glob(os.path.join(gds_dir, "**", f"{cell_name}.gds"), recursive=True)
    if matches:
        return sorted(matches)[0]
    return None


def choose_fixed_path_for_device(device, fixed_index, fixed_lower_index):
    cell = device["cell"]

    # Exact case-sensitive match first.
    if cell in fixed_index:
        return sorted(fixed_index[cell])[0]

    # Then case-insensitive match. Useful for W/L uppercase naming variants.
    if cell.lower() in fixed_lower_index:
        return sorted(fixed_lower_index[cell.lower()])[0]

    # If BJT is written as base model + W/L params, synthesize exact fixed-device name.
    low = cell.lower()
    w = device.get("w_um")
    l = device.get("l_um")
    if is_bjt(cell) and w is not None and l is not None:
        wt = number_to_tag(w, 2)
        lt = number_to_tag(l, 2)
        for base in [cell, cell.replace("_w", "_W")]:
            trial = re.sub(r"_w\d+p\d+l\d+p\d+$", "", base, flags=re.I)
            trial = f"{trial}_W{wt}L{lt}"
            if trial in fixed_index:
                return sorted(fixed_index[trial])[0]
            if trial.lower() in fixed_lower_index:
                return sorted(fixed_lower_index[trial.lower()])[0]

    return None


def load_cell_from_gds(layout, gds_path, wanted_cell_name, loaded_gds):
    if not gds_path or not os.path.isfile(gds_path):
        return None, f"GDS path missing: {gds_path}"

    if gds_path not in loaded_gds:
        print("Reading GDS:", gds_path)
        layout.read(gds_path)
        loaded_gds.add(gds_path)

    # Exact cell name first.
    cell = layout.cell(wanted_cell_name)
    if cell is not None:
        return cell, None

    # Case-insensitive fallback.
    wanted_low = wanted_cell_name.lower()
    for c in layout.each_cell():
        if c.name.lower() == wanted_low:
            return c, None

    # Per-cell GDS often has the basename as top cell. Try that.
    stem = os.path.splitext(os.path.basename(gds_path))[0]
    cell = layout.cell(stem)
    if cell is not None:
        return cell, None

    # Last resort: pick the largest top-like cell imported from that file.
    # This is not perfect but prevents silent failure for IO/odd GDS naming.
    top_candidates = [c for c in layout.each_cell() if c.name.lower() == stem.lower()]
    if top_candidates:
        return top_candidates[0], None

    return None, f"Cell '{wanted_cell_name}' not found inside {gds_path}"


def wrap_multiplicity(layout, base_cell, device):
    mult = int(device.get("m") or 1)
    if mult <= 1:
        return base_cell, {"m_handling": "single", "m": 1}

    wrap_name = sanitize_cell_name(f"{device['inst']}_{base_cell.name}_m{mult}_WRAP")
    existing = layout.cell(wrap_name)
    if existing is not None:
        return existing, {"m_handling": "existing_wrapper", "m": mult}

    wrap = layout.create_cell(wrap_name)
    bbox = base_cell.bbox()
    step = max(bbox.width(), dbu_um(layout, 1.0)) + dbu_um(layout, M_SPACING_UM)
    for i in range(mult):
        wrap.insert(pya.CellInstArray(base_cell.cell_index(), pya.Trans(i * step, 0)))
    return wrap, {"m_handling": "wrapper_array", "m": mult, "x_step_dbu": step}

# -----------------------------------------------------------------------------
# gdsfactory / SKY130 draw backend compatibility layer
# -----------------------------------------------------------------------------



def install_gf_boolean_compat(gf):
    """Provide gf.boolean for SKY130 draw_fet.py guard-ring path.

    Some gdsfactory versions used with KLayout/Python 3.12 do not expose
    gdsfactory.boolean at the top level, but SKY130 draw_fet.py calls it when
    bulk="guard ring". This compatibility shim supports the A-B boolean
    calls used by draw_fet.py by using gdstk directly.
    """
    if hasattr(gf, "boolean"):
        return

    def _collect_polygons(obj):
        if obj is None:
            return []
        # gdsfactory Component / ComponentReference usually supports get_polygons.
        try:
            polys = obj.get_polygons(by_spec=False)
        except TypeError:
            try:
                polys = obj.get_polygons()
            except Exception:
                polys = []
        except Exception:
            polys = []

        out = []
        for poly in polys or []:
            try:
                pts = poly.points
            except Exception:
                pts = poly
            if pts is not None:
                out.append(pts)
        return out

    def _boolean(A=None, B=None, operation="A-B", layer=None, **kwargs):
        try:
            import gdstk
        except Exception as e:
            raise RuntimeError(
                "draw_fet guard ring needs boolean support, but gdstk could not be imported: %s" % e
            )

        op_txt = str(operation).strip().lower().replace(" ", "")
        op_map = {
            "a-b": "not",
            "not": "not",
            "a+b": "or",
            "or": "or",
            "a|b": "or",
            "a&b": "and",
            "and": "and",
            "a*b": "and",
            "a^b": "xor",
            "xor": "xor",
        }
        op = op_map.get(op_txt, op_txt)

        polys_a = _collect_polygons(A)
        polys_b = _collect_polygons(B)
        result = gdstk.boolean(polys_a, polys_b, op, precision=1e-4)

        c = gf.Component()
        if layer is None:
            layer = (1, 0)
        for poly in result or []:
            pts = getattr(poly, "points", poly)
            c.add_polygon(pts, layer=layer)
        return c

    gf.boolean = _boolean


def prepare_gdsfactory_runtime():
    try:
        import gdsfactory as gf
    except Exception as e:
        return None, f"Could not import gdsfactory. Set KLAYOUT_PY_SITE to your gf7 venv site-packages. Error: {e}"

    # SKY130 draw_fet.py guard-ring branch calls gf.boolean.
    # The gf7 environment used with Mint/KLayout may not expose this symbol.
    install_gf_boolean_compat(gf)

    # Activate a generic PDK when available. Some gf versions need this before write_gds.
    try:
        from gdsfactory.generic_tech import get_generic_pdk
        get_generic_pdk().activate()
    except Exception:
        try:
            from gdsfactory.generic_tech import get_generic_pdk
            pdk = get_generic_pdk()
            if hasattr(pdk, "activate"):
                pdk.activate()
        except Exception:
            pass

    # Some older SKY130 helper code imports sky130A.PDK. Provide a harmless shim.
    try:
        sky_mod = sys.modules.get("sky130A")
        if sky_mod is None:
            sky_mod = types.ModuleType("sky130A")
            sys.modules["sky130A"] = sky_mod
        if not hasattr(sky_mod, "PDK"):
            try:
                sky_mod.PDK = gf.get_active_pdk()
            except Exception:
                sky_mod.PDK = None
    except Exception:
        pass

    return gf, None


def safe_open_component(name="sky130_generated"):
    gf, err = prepare_gdsfactory_runtime()
    if err:
        raise RuntimeError(err)
    global _GF_UNIQUE_COUNTER
    _GF_UNIQUE_COUNTER += 1
    unique = f"{sanitize_cell_name(name)}_{os.getpid()}_{int(time.time()*1000000)%100000000}_{_GF_UNIQUE_COUNTER}_{random.randint(0,9999)}"
    return gf.Component(unique)


def import_gf_component_as_klayout_cell(layout, component, wanted_name):
    fd, tmp_gds = tempfile.mkstemp(prefix="sky130_gf_bridge_", suffix=".gds")
    os.close(fd)
    wanted_name = sanitize_cell_name(wanted_name)

    try:
        try:
            component.name = wanted_name
        except Exception:
            pass

        before_names = set(c.name for c in layout.each_cell())
        component.write_gds(tmp_gds)
        layout.read(tmp_gds)
        after_names = set(c.name for c in layout.each_cell())

        cell = layout.cell(wanted_name)
        if cell is not None:
            return cell

        new_names = list(after_names - before_names)
        candidates = [layout.cell(n) for n in new_names if layout.cell(n) is not None]
        if candidates:
            return max(candidates, key=lambda c: c.bbox().width() * c.bbox().height())

        raise RuntimeError("gdsfactory component wrote a GDS, but no new KLayout cell was found")
    finally:
        try:
            os.remove(tmp_gds)
        except Exception:
            pass


def install_take_component_bridge(module):
    """Patch SKY130 draw modules so they work with modern gdsfactory/kfactory."""
    if module is None:
        return

    def bridge_take_component(component, target):
        # If target is a gf.Component, add generated component into it.
        if hasattr(target, "add_ref"):
            try:
                target.add_ref(component)
                return target
            except Exception:
                pass
            try:
                target << component
                return target
            except Exception:
                pass

        # If target is a KLayout cell, import generated component through temp GDS.
        if hasattr(target, "layout") and hasattr(target, "insert"):
            imported = import_gf_component_as_klayout_cell(target.layout(), component, component.name)
            target.insert(pya.CellInstArray(imported.cell_index(), pya.Trans(0, 0)))
            return target

        raise RuntimeError("Unsupported take_component target type: %s" % type(target))

    try:
        module.open_component = safe_open_component
    except Exception:
        pass
    try:
        module.take_component = bridge_take_component
    except Exception:
        pass


def import_cells_module(module_name):
    if CELLS_PY_ROOT is None:
        return None, "SKY130 KLayout python/cells folder not found"
    if CELLS_PY_ROOT not in sys.path:
        sys.path.insert(0, CELLS_PY_ROOT)

    gf, err = prepare_gdsfactory_runtime()
    if err:
        return None, err

    try:
        # Patch pdk first before draw modules call it.
        try:
            pdk_mod = importlib.import_module("cells.pdk")
            install_take_component_bridge(pdk_mod)
        except Exception:
            pass

        try:
            parent_res_mod = importlib.import_module("cells.parent_res")
            install_take_component_bridge(parent_res_mod)
        except Exception:
            pass

        mod = importlib.import_module(module_name)
        install_take_component_bridge(mod)
        return mod, None
    except Exception as e:
        return None, f"Could not import {module_name}: {e}"


def call_generator_safely(func, kwargs):
    sig = inspect.signature(func)
    allowed = set(sig.parameters.keys())
    clean_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
    return func(**clean_kwargs), clean_kwargs


# -----------------------------------------------------------------------------
# Magic-backed guard-ring MOS generation
# -----------------------------------------------------------------------------

def tcl_brace(value):
    """Return a Tcl-safe braced string."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace("}", "\\}").replace("{", "\\{")
    return "{" + s + "}"


def tcl_name(value):
    """Conservative Tcl/Magic-safe identifier for cell and instance names."""
    return sanitize_cell_name(value).replace(".", "_").replace("$", "_")


def magic_param_value(device, key, fallback=None):
    """Use evaluated microns for W/L and plain integers for nf/m.

    Magic's netlist_to_layout.py passes raw SPICE strings, but for our one-device
    temporary netlists raw expressions such as {S1} would not have the original
    .param context. The parser has already evaluated those into device['w_um'] and
    device['l_um'], so we pass explicit micron numbers here.
    """
    if key == "w":
        val = device.get("w_um")
        if val is None:
            val = fallback
        return None if val is None else ("%.12g" % float(val))
    if key == "l":
        val = device.get("l_um")
        if val is None:
            val = fallback
        return None if val is None else ("%.12g" % float(val))
    if key == "nf":
        return str(int(device.get("nf") or 1))
    if key == "m":
        # Keep the Magic-generated guarded cell as ONE physical device.
        # The common wrapper below handles SPICE m>1 so we do not double-array it.
        return "1"
    params = device.get("params_raw", {}) or {}
    val = params.get(key)
    if val is None:
        return fallback
    return str(val).strip().strip("'\"")


def build_magic_gencell_param_list(device):
    """Build the -spice key/value list for magic::gencell."""
    cell_name = device.get("cell", "")
    params = []

    # W/L defaults match the old direct-draw path only when netlist did not give them.
    default_l = 0.5 if "g5v0" in cell_name else 0.15
    default_w = 0.42
    for key, default in (("w", default_w), ("l", default_l), ("nf", "1"), ("m", "1")):
        val = magic_param_value(device, key, default)
        if val is not None:
            params.extend([key, val])

    # Preserve simple extra MOS generator params when present. Do not pass bulk/grw:
    # Magic's SKY130 toolkit/import path makes the guarded device geometry itself.
    passthrough = [
        "ad", "as", "pd", "ps", "nrd", "nrs", "sa", "sb", "sd",
        "mult", "sdf", "area", "pj", "int_s", "nfinger",
    ]
    raw = device.get("params_raw", {}) or {}
    for key in passthrough:
        if key in raw and key not in {p for p in params[0::2]}:
            params.extend([key, str(raw[key]).strip().strip("'\"")])
    return params


def import_gds_pick_new_cell(layout, gds_path, wanted_cell_name):
    """Read a just-created GDS and return the requested top cell or largest new cell."""
    if not gds_path or not os.path.isfile(gds_path):
        return None, f"Magic GDS was not created: {gds_path}"

    before = set(c.name for c in layout.each_cell())
    print("Reading Magic guard-ring GDS:", gds_path)
    layout.read(gds_path)

    cell = layout.cell(wanted_cell_name)
    if cell is not None:
        return cell, None

    wanted_low = wanted_cell_name.lower()
    for c in layout.each_cell():
        if c.name.lower() == wanted_low:
            return c, None

    after = set(c.name for c in layout.each_cell())
    new_names = list(after - before)
    candidates = [layout.cell(n) for n in new_names if layout.cell(n) is not None]
    if candidates:
        return max(candidates, key=lambda c: max(1, c.bbox().width()) * max(1, c.bbox().height())), None

    return None, f"No new cell was found after reading Magic GDS {gds_path}"


def generate_magic_guard_ring_mos(layout, device):
    """Generate a selected MOSFET using Magic's SKY130 gencell/import backend.

    This intentionally avoids the KLayout/gdsfactory draw_fet.py guard-ring branch,
    which produced DRC errors in Magic for the user's large devices. The Tcl below
    mirrors the official open_pdks netlist_to_layout flow: load a top cell, call
    magic::gencell ${PDKNAMESPACE}::<device> <inst> -spice ..., then export GDS.
    """
    if SKY_GR_BACKEND not in ("magic", "auto"):
        return None, "Magic guard-ring backend disabled by SKY_GR_BACKEND", None

    if not shutil.which(SKY_MAGIC_BIN):
        return None, f"Magic executable not found: {SKY_MAGIC_BIN}", {"backend": "magic_guard_ring_mos"}

    if not os.path.isfile(SKY_MAGIC_RC):
        return None, f"Magic rcfile not found: {SKY_MAGIC_RC}", {"backend": "magic_guard_ring_mos"}

    cell_name = device["cell"]
    inst_name = tcl_name(device.get("inst", "X"))
    top_name = tcl_name(f"MAGIC_GR_{device.get('inst', 'X')}_{cell_name}")

    workdir = tempfile.mkdtemp(prefix=f"sky130_magic_gr_{inst_name}_")
    out_gds = os.path.join(workdir, f"{top_name}.gds")
    tcl_path = os.path.join(workdir, "make_guard_ring_mos.tcl")

    params = build_magic_gencell_param_list(device)
    param_tcl = " ".join([tcl_brace(p) for p in params])

    # The PDK normally sets PDKNAMESPACE. sky130 is the namespace used by the
    # open_pdks netlist_to_layout examples; keep it as a fallback.
    tcl = f"""
# Auto-generated by skyspice2klayout_all_devices.py
# Guard-ring MOS generated through Magic SKY130 toolkit.
drc off
if {{![info exists PDKNAMESPACE]}} {{
    if {{[namespace exists ::sky130]}} {{
        set PDKNAMESPACE sky130
    }} elseif {{[namespace exists ::sky130A]}} {{
        set PDKNAMESPACE sky130A
    }} else {{
        puts stderr "MAGIC_GR_ERROR: PDKNAMESPACE is not defined and no sky130 namespace exists"
        quit -noprompt
        exit 2
    }}
}}
puts "MAGIC_GR_INFO: PDKNAMESPACE=$PDKNAMESPACE"
load {tcl_brace(top_name)} -quiet
box 0um 0um 0um 0um
set devtype ${{PDKNAMESPACE}}::{cell_name}
puts "MAGIC_GR_INFO: magic::gencell $devtype {inst_name} -spice {param_tcl}"
if {{[catch {{magic::gencell $devtype {inst_name} -spice {param_tcl}}} msg]}} {{
    puts stderr "MAGIC_GR_ERROR: magic::gencell failed: $msg"
    quit -noprompt
    exit 3
}}
load {tcl_brace(top_name)} -quiet
writeall force
gds write {tcl_brace(out_gds)}
quit -noprompt
"""
    with open(tcl_path, "w") as f:
        f.write(tcl)

    env = os.environ.copy()
    env["MAGTYPE"] = "mag"
    env.setdefault("PDK_ROOT", PDK_ROOT)
    env.setdefault("PDK", PDK)
    env.setdefault("PDKPATH", PDKPATH)

    try:
        proc = subprocess.run(
            [SKY_MAGIC_BIN, "-dnull", "-noconsole", "-rcfile", SKY_MAGIC_RC, tcl_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            universal_newlines=True,
            timeout=float(os.environ.get("SKY_MAGIC_TIMEOUT_S", "90")),
        )
    except Exception as e:
        if not SKY_GR_MAGIC_KEEP:
            shutil.rmtree(workdir, ignore_errors=True)
        return None, f"Magic guard-ring generation launch failed: {e}", {
            "backend": "magic_guard_ring_mos",
            "workdir": workdir,
            "tcl": tcl_path,
        }

    detail = {
        "backend": "magic_guard_ring_mos",
        "magic_bin": SKY_MAGIC_BIN,
        "magic_rc": SKY_MAGIC_RC,
        "workdir": workdir if SKY_GR_MAGIC_KEEP else None,
        "tcl": tcl_path if SKY_GR_MAGIC_KEEP else None,
        "gds_path": out_gds if SKY_GR_MAGIC_KEEP else None,
        "params_used": params,
        "stdout_tail": (proc.stdout or "").splitlines()[-20:],
        "stderr_tail": (proc.stderr or "").splitlines()[-20:],
        "returncode": proc.returncode,
    }

    if proc.returncode != 0:
        if not SKY_GR_MAGIC_KEEP:
            shutil.rmtree(workdir, ignore_errors=True)
        return None, "Magic guard-ring generation failed; stderr tail: " + " | ".join(detail["stderr_tail"]), detail

    cell, err = import_gds_pick_new_cell(layout, out_gds, top_name)
    if cell is None:
        if not SKY_GR_MAGIC_KEEP:
            shutil.rmtree(workdir, ignore_errors=True)
        return None, err, detail

    if not SKY_GR_MAGIC_KEEP:
        shutil.rmtree(workdir, ignore_errors=True)

    wrapped, wrap_info = wrap_multiplicity(layout, cell, device)
    detail["wrap"] = wrap_info
    detail["top_cell"] = cell.name
    return wrapped, None, detail


def generate_with_gf(layout, device, generator_kind):
    # MOS backend selection:
    #   SKY_MOS_BACKEND=gdsfactory -> all MOS through gdsfactory direct draw
    #   SKY_MOS_BACKEND=hybrid     -> selected guarded MOS through Magic, others through gdsfactory
    #   SKY_MOS_BACKEND=magic      -> selection-safe Magic mode:
    #                                selected guarded MOS through Magic, others through gdsfactory.
    #                                Set SKY_MAGIC_ALL_GUARD_RINGS=1 to intentionally
    #                                force every MOS through Magic/guard-ring generation.
    mos_backend = os.environ.get("SKY_MOS_BACKEND", "hybrid").strip().lower()
    if mos_backend in ("gf", "gdsfactory", "direct", "draw"):
        use_magic_for_this_mos = False
    elif mos_backend in ("magic", "magic_selected", "m"):
        use_magic_for_this_mos = generator_kind == "mos" and (bool(device.get("guard_ring")) or MAGIC_ALL_GUARD_RINGS)
    elif mos_backend in ("magic_all", "all"):
        use_magic_for_this_mos = generator_kind == "mos"
    else:
        use_magic_for_this_mos = generator_kind == "mos" and bool(device.get("guard_ring"))

    if use_magic_for_this_mos and SKY_GR_BACKEND in ("magic", "auto"):
        cell, err, detail = generate_magic_guard_ring_mos(layout, device)
        if cell is not None:
            return cell, None, detail
        if SKY_GR_BACKEND == "magic" and not SKY_GR_MAGIC_FALLBACK_DIRECT:
            return None, err, detail
        print("WARNING: Magic guard-ring generation failed; falling back to direct draw:", err)

    gf, err = prepare_gdsfactory_runtime()
    if err:
        return None, err, None

    cell_name = device["cell"]
    base_name = sanitize_cell_name(
        f"GEN_{device['inst']}_{cell_name}_W{device.get('w_um')}_L{device.get('l_um')}_nf{device.get('nf')}"
    )
    gf_cell = gf.Component(base_name + "_GF")

    params = device.get("params_raw", {})
    w = device.get("w_um")
    l = device.get("l_um")
    nf = int(device.get("nf") or 1)

    try:
        if generator_kind == "mos":
            mod, err = import_cells_module("cells.draw_fet")
            if err:
                return None, err, None
            kind = mos_kind(cell_name)
            func = getattr(mod, "draw_pfet" if kind == "pfet" else "draw_nfet", None)
            if func is None:
                return None, "draw_pfet/draw_nfet not found", None
            kwargs = {
                "cell": gf_cell,
                "type": cell_name,
                "l": float(l if l is not None else (0.5 if "g5v0" in cell_name else 0.15)),
                "w": float(w if w is not None else 0.42),
                "nf": max(nf, 1),
                "bulk": "guard ring" if device.get("guard_ring") else params.get("bulk", "None"),
                "gate_con_pos": params.get("gate_con_pos", "alternating"),
                "sd_con_col": parse_int(params.get("sd_con_col"), 1),
                "inter_sd_l": parse_numeric(params.get("inter_sd_l"), 0.5 if kind == "pfet" else 0.3),
                "con_bet_fin": parse_boolish(params.get("con_bet_fin"), 1),
                "grw": parse_numeric(params.get("grw"), SKY_GRW_UM),
                "interdig": parse_int(params.get("interdig"), 0),
                "patt": params.get("patt", ""),
            }

        elif generator_kind == "diode":
            mod, err = import_cells_module("cells.draw_diode")
            if err:
                return None, err, None
            if "photodiode" in cell_name.lower():
                func = getattr(mod, "draw_photodiode", None)
                kwargs = {"cell": gf_cell, "device_name": cell_name}
            else:
                func = getattr(mod, "draw_diode", None)
                d_type = "p" if "pd2" in cell_name.lower() or "ps" in cell_name.lower() else "n"
                kwargs = {
                    "cell": gf_cell,
                    "d_type": d_type,
                    "type": cell_name,
                    "l": float(l if l is not None else 0.45),
                    "w": float(w if w is not None else 0.45),
                    "cath_w": parse_numeric(params.get("cath_w"), 0.17),
                    "grw": parse_numeric(params.get("grw"), 0.17),
                }
            if func is None:
                return None, "draw_diode/draw_photodiode not found", None

        elif generator_kind == "cap":
            low = cell_name.lower()
            if "cap_vpp" in low:
                # VPP should normally be static exact GDS; this is only a fallback.
                mod, err = import_cells_module("cells.draw_vpp")
                if err:
                    return None, err, None
                func = getattr(mod, "draw_vpp", None)
                kwargs = {"cell": gf_cell, "device_name": cell_name}
            elif "cap_var" in low:
                mod, err = import_cells_module("cells.draw_cap")
                if err:
                    return None, err, None
                func = getattr(mod, "draw_cap_var", None)
                kwargs = {
                    "cell": gf_cell,
                    "type": cell_name,
                    "l": float(l if l is not None else 0.18),
                    "w": float(w if w is not None else 1.0),
                    "nf": max(nf, 1),
                    "tap_con_col": parse_int(params.get("tap_con_col"), 1),
                    "gr": parse_boolish(params.get("gr"), 1),
                    "grw": parse_numeric(params.get("grw"), 0.17),
                }
            elif "cap_mim_m3_2" in low or "model__cap_mim_m4" in low:
                mod, err = import_cells_module("cells.draw_cap")
                if err:
                    return None, err, None
                func = getattr(mod, "draw_mim_cap", None)
                kwargs = {
                    "cell": gf_cell,
                    "type": "sky130_fd_pr__model__cap_mim_m4",
                    "l": float(l if l is not None else 2.16),
                    "w": float(w if w is not None else 2.16),
                }
            else:
                mod, err = import_cells_module("cells.draw_cap")
                if err:
                    return None, err, None
                func = getattr(mod, "draw_mim_cap", None)
                kwargs = {
                    "cell": gf_cell,
                    "type": "sky130_fd_pr__model__cap_mim",
                    "l": float(l if l is not None else 2.0),
                    "w": float(w if w is not None else 2.0),
                }
            if func is None:
                return None, "cap draw function not found", None

        elif generator_kind == "res":
            low = cell_name.lower()
            if "res_generic_m" in low or "res_generic_l1" in low:
                mod, err = import_cells_module("cells.res_metal_child")
                if err:
                    return None, err, None
                cls = getattr(mod, "res_metal_draw", None)
            elif "res_nd" in low or "res_pd" in low:
                mod, err = import_cells_module("cells.res_diff_child")
                if err:
                    return None, err, None
                cls = getattr(mod, "res_diff_draw", None)
            else:
                mod, err = import_cells_module("cells.res_poly_child")
                if err:
                    return None, err, None
                cls = getattr(mod, "res_poly_draw", None)
            if cls is None:
                return None, "resistor draw class not found", None
            drw = cls(cell_name)
            # child module uses parent_res.take_component; patch already installed.
            kwargs = {
                "cell": gf_cell,
                "type": cell_name,
                "l": float(l if l is not None else parse_numeric(params.get("len"), 1.65)),
                "w": float(w if w is not None else parse_numeric(params.get("w"), 0.42)),
                "gr": parse_boolish(params.get("gr"), 1),
            }
            ret, used_kwargs = call_generator_safely(drw.your_res, kwargs)
            imported = import_gf_component_as_klayout_cell(layout, gf_cell, base_name)
            wrapped, wrap_info = wrap_multiplicity(layout, imported, device)
            return wrapped, None, {
                "backend": "sky130_res_direct_draw",
                "generator": cls.__name__,
                "params_used": json_safe(used_kwargs),
                "wrap": wrap_info,
            }

        else:
            return None, f"Unknown generator kind: {generator_kind}", None

        ret, used_kwargs = call_generator_safely(func, kwargs)
        # Most uploaded SKY130 draw functions modify gf_cell through patched take_component.
        # Some might return a component directly. Prefer returned component only if usable.
        component_to_import = ret if hasattr(ret, "write_gds") else gf_cell
        imported = import_gf_component_as_klayout_cell(layout, component_to_import, base_name)
        wrapped, wrap_info = wrap_multiplicity(layout, imported, device)
        return wrapped, None, {
            "backend": f"sky130_{generator_kind}_direct_draw",
            "generator": getattr(func, "__name__", str(func)),
            "params_used": json_safe({k: v for k, v in used_kwargs.items() if k != "cell"}),
            "wrap": wrap_info,
        }

    except Exception as e:
        return None, f"{generator_kind} generation failed: {e}", {
            "traceback": traceback.format_exc(limit=8)
        }

# -----------------------------------------------------------------------------
# Device creation order
# -----------------------------------------------------------------------------


def create_layout_cell_for_device(layout, device, loaded_gds, fixed_index, fixed_lower_index):
    cell_name = device["cell"]

    # 1) Exact static fixed devices. This covers BJT, RF MOS/BJT, VPP caps, photodiode, coils.
    fixed_path = choose_fixed_path_for_device(device, fixed_index, fixed_lower_index)
    if fixed_path:
        cell, err = load_cell_from_gds(layout, fixed_path, cell_name, loaded_gds)
        if cell is not None:
            wrapped, wrap_info = wrap_multiplicity(layout, cell, device)
            return wrapped, "static_fixed_gds", {
                "gds_path": fixed_path,
                "wrap": wrap_info,
            }
        static_err = err
    else:
        static_err = "No exact fixed-device GDS match"

    # 2) libs.ref combined/exact GDS. This covers standard cells and IO files.
    libsref_path = find_libsref_gds_for_cell(cell_name)
    if libsref_path and (is_standard_cell(cell_name) or is_io_cell(cell_name)):
        cell, err = load_cell_from_gds(layout, libsref_path, cell_name, loaded_gds)
        if cell is not None:
            wrapped, wrap_info = wrap_multiplicity(layout, cell, device)
            return wrapped, "libsref_gds", {
                "gds_path": libsref_path,
                "wrap": wrap_info,
            }
        libsref_err = err
    else:
        libsref_err = "No libs.ref GDS candidate or not a std/IO cell"

    # 3) Procedural generated devices.
    if is_regular_pr_mos(cell_name):
        cell, err, detail = generate_with_gf(layout, device, "mos")
        if cell is not None:
            return cell, "direct_draw_mos", detail
        draw_err = err
    elif is_diode(cell_name):
        cell, err, detail = generate_with_gf(layout, device, "diode")
        if cell is not None:
            return cell, "direct_draw_diode", detail
        draw_err = err
    elif is_cap(cell_name):
        cell, err, detail = generate_with_gf(layout, device, "cap")
        if cell is not None:
            return cell, "direct_draw_cap", detail
        draw_err = err
    elif is_res(cell_name):
        cell, err, detail = generate_with_gf(layout, device, "res")
        if cell is not None:
            return cell, "direct_draw_res", detail
        draw_err = err
    else:
        draw_err = "No direct-draw backend selected"

    # 4) Last fallback: try libs.ref even for primitive combined sky130_fd_pr.gds.
    if libsref_path:
        cell, err = load_cell_from_gds(layout, libsref_path, cell_name, loaded_gds)
        if cell is not None:
            wrapped, wrap_info = wrap_multiplicity(layout, cell, device)
            return wrapped, "libsref_fallback_gds", {"gds_path": libsref_path, "wrap": wrap_info}
        libsref_err = err

    return None, None, {
        "static_fixed": static_err,
        "libsref": libsref_err,
        "direct_draw": draw_err,
    }


# -----------------------------------------------------------------------------
# Analog unit-array / finger-array synthesis
# -----------------------------------------------------------------------------

_UNIT_CELL_CACHE = {}


def unit_array_enabled():
    return UNIT_ARRAY_MODE not in ("0", "false", "no", "off", "none", "disable", "disabled")


def unit_array_layout_enabled():
    return UNIT_ARRAY_MODE in ("layout", "physical", "replace", "on", "yes", "1", "true")


def unit_array_plan_for_device(device):
    """Return a physical unit-array plan for one large MOS, or None.

    The plan is conservative: it only splits MOS width into parallel unit devices
    with the same D/G/S/B nets. This is electrically equivalent at schematic
    level, but LVS may report multiple extracted MOS devices rather than one
    W=total device. Therefore the output report keeps original-instance mapping.
    """
    if not unit_array_enabled():
        return None
    if not is_regular_pr_mos(device.get("cell", "")):
        return None
    w = device.get("w_um")
    l = device.get("l_um")
    if w is None or l is None:
        return None
    try:
        w = float(w)
        l = float(l)
    except Exception:
        return None
    if w < UNIT_ARRAY_MIN_W_UM:
        return None
    base_unit = max(float(UNIT_W_UM), 0.05)
    units = max(1, int(round(w / base_unit)))
    if units < 2:
        return None
    if units > UNIT_ARRAY_MAX_UNITS:
        units = UNIT_ARRAY_MAX_UNITS
    unit_w = w / units
    # Auto rows: keep arrays readable without making a very long snake.
    if UNIT_ARRAY_ROWS and UNIT_ARRAY_ROWS > 0:
        rows = max(1, min(UNIT_ARRAY_ROWS, units))
    elif units >= 24:
        rows = 4
    elif units >= 8:
        rows = 2
    else:
        rows = 1
    cols = int(math.ceil(units / rows))
    return {
        "enabled": True,
        "inst": device.get("inst"),
        "cell": device.get("cell"),
        "kind": mos_kind(device.get("cell", "")),
        "original_w_um": w,
        "unit_w_um": unit_w,
        "l_um": l,
        "units": units,
        "rows": rows,
        "cols": cols,
        "nf_each": int(device.get("nf") or 1),
        "guard_ring": bool(device.get("guard_ring")),
        "mode": UNIT_ARRAY_MODE,
        "notes": [
            "Original MOS is represented as parallel unit devices with same D/G/S/B nets.",
            "Use this for analog planning/matching. For strict LVS, compare summed W or keep mode=plan/off."
        ],
    }


def unit_array_serpentine_positions(plan, unit_bbox_um):
    """Return local positions for each unit in a common-centroid-friendly snake.

    For a single transistor being split into units, true ABBA common centroid is
    not meaningful by itself, but a serpentine two/four-row pattern improves
    visual symmetry and keeps routing shorter than one long row.
    """
    uw, uh = unit_bbox_um
    positions = []
    idx = 0
    for r in range(plan["rows"]):
        col_range = range(plan["cols"])
        if r % 2 == 1:
            col_range = reversed(range(plan["cols"]))
        for c in col_range:
            if idx >= plan["units"]:
                break
            x = c * (uw + UNIT_ARRAY_GAP_UM)
            y = r * (uh + UNIT_ARRAY_ROW_GAP_UM)
            positions.append({"unit_index": idx + 1, "row": r + 1, "col": c + 1, "x_um": x, "y_um": y})
            idx += 1
    return positions


def make_unit_device_from_plan(device, plan):
    d = dict(device)
    d["inst"] = f"{device.get('inst')}_UNIT"
    d["w_um"] = plan["unit_w_um"]
    d["l_um"] = plan["l_um"]
    d["nf"] = plan.get("nf_each", 1)
    # Force one physical copy for the unit cell; the wrapper arrays it.
    d["m"] = 1
    raw_params = dict(device.get("params_raw", {}) or {})
    raw_params["w"] = "%.12g" % float(plan["unit_w_um"])
    raw_params["l"] = "%.12g" % float(plan["l_um"])
    raw_params["nf"] = str(int(plan.get("nf_each", 1)))
    raw_params["m"] = "1"
    d["params_raw"] = raw_params
    if UNIT_ARRAY_USE_MAGIC_GR and device.get("guard_ring"):
        d["guard_ring"] = True
    return d


def unit_cell_cache_key(device, plan):
    nodes = tuple(device.get("nodes", []))
    # Include guard ring and dimensions, but not original instance, so identical
    # units can reuse the same generated cell when safe.
    return (
        device.get("cell"),
        round(float(plan["unit_w_um"]), 6),
        round(float(plan["l_um"]), 6),
        int(plan.get("nf_each", 1)),
        bool(device.get("guard_ring") and UNIT_ARRAY_USE_MAGIC_GR),
        nodes,
    )


def create_unit_array_cell(layout, entry, loaded_gds, fixed_index, fixed_lower_index):
    """Replace a large MOS entry cell by a wrapper containing many unit cells."""
    device = entry["device"]
    plan = unit_array_plan_for_device(device)
    if not plan:
        return None

    if not unit_array_layout_enabled():
        entry["unit_array_plan"] = plan
        device["unit_array_plan"] = {k: v for k, v in plan.items() if k != "notes"}
        return plan

    key = unit_cell_cache_key(device, plan)
    unit_cell = _UNIT_CELL_CACHE.get(key)
    unit_detail = None
    unit_method = None
    if unit_cell is None:
        unit_dev = make_unit_device_from_plan(device, plan)
        unit_cell, unit_method, unit_detail = create_layout_cell_for_device(
            layout, unit_dev, loaded_gds, fixed_index, fixed_lower_index
        )
        if unit_cell is None:
            plan["layout_created"] = False
            plan["error"] = "Could not generate unit cell"
            plan["detail"] = json_safe(unit_detail)
            entry["unit_array_plan"] = plan
            return plan
        _UNIT_CELL_CACHE[key] = unit_cell
    else:
        unit_method = "cached_unit_cell"
        unit_detail = {"cached": True, "unit_cell": unit_cell.name}

    ub = unit_cell.bbox()
    unit_w_bbox = max(1.0, ub.width() * layout.dbu)
    unit_h_bbox = max(1.0, ub.height() * layout.dbu)
    local_positions = unit_array_serpentine_positions(plan, (unit_w_bbox, unit_h_bbox))

    wrap_name = sanitize_cell_name(
        f"UNITARR_{device.get('inst')}_{plan['units']}x_W{number_to_tag(plan['unit_w_um'],3)}_L{number_to_tag(plan['l_um'],3)}"
    )
    wrapper = layout.cell(wrap_name)
    if wrapper is None:
        wrapper = layout.create_cell(wrap_name)
        for pos in local_positions:
            tx = dbu_um(layout, pos["x_um"]) - ub.left
            ty = dbu_um(layout, pos["y_um"]) - ub.bottom
            wrapper.insert(pya.CellInstArray(unit_cell.cell_index(), pya.Trans(tx, ty)))
            # Small non-fab unit index label.
            if ADD_LABELS:
                add_text(layout, wrapper, layout.layer(*UNIT_ARRAY_TEXT_LAYER), f"u{pos['unit_index']}", pos["x_um"], pos["y_um"] - 0.8, 0.45)
        if ADD_LABELS:
            add_text(
                layout, wrapper, layout.layer(*UNIT_ARRAY_TEXT_LAYER),
                f"{device.get('inst')} UNIT ARRAY: {plan['units']} x W={plan['unit_w_um']:.4g} L={plan['l_um']:.4g}",
                0.0, -2.2, 0.65,
            )
        # Non-fab dummy markers at the left/right array edges.  These are only
        # reminders; they are not fabricated dummy transistors.
        if UNIT_ARRAY_ADD_DUMMY_MARKERS and ADD_LABELS:
            dummy_layer = layout.layer(*UNIT_ARRAY_DUMMY_LAYER)
            arr_bbox = wrapper.bbox()
            margin = dbu_um(layout, 1.5)
            dummy_w = dbu_um(layout, 0.6)
            wrapper.shapes(dummy_layer).insert(pya.Box(arr_bbox.left - margin - dummy_w, arr_bbox.bottom, arr_bbox.left - margin, arr_bbox.top))
            wrapper.shapes(dummy_layer).insert(pya.Box(arr_bbox.right + margin, arr_bbox.bottom, arr_bbox.right + margin + dummy_w, arr_bbox.top))
            add_text(layout, wrapper, layout.layer(*UNIT_ARRAY_TEXT_LAYER), "DUMMY MARKERS", arr_bbox.left * layout.dbu - 2.5, arr_bbox.top * layout.dbu + 1.0, 0.45)

    plan.update({
        "layout_created": True,
        "wrapper_cell": wrapper.name,
        "unit_cell": unit_cell.name,
        "unit_method": unit_method,
        "unit_detail": json_safe(unit_detail),
        "unit_bbox_um": {"w_um": unit_w_bbox, "h_um": unit_h_bbox},
        "unit_positions": local_positions,
        "dummy_markers_non_fab": bool(UNIT_ARRAY_ADD_DUMMY_MARKERS),
    })
    entry["base_cell_before_unit_array"] = entry.get("cell")
    entry["cell"] = wrapper
    entry["method"] = str(entry.get("method", "")) + "+unit_array"
    entry["unit_array_plan"] = plan
    device["unit_array_plan"] = {k: v for k, v in plan.items() if k not in ("unit_positions", "unit_detail")}
    device["unit_array_units"] = plan["units"]
    device["unit_array_unit_w_um"] = plan["unit_w_um"]
    return plan


def apply_unit_array_synthesis(layout, entries, loaded_gds, fixed_index, fixed_lower_index):
    """Apply optional unit-array synthesis to large MOS entries."""
    report = {
        "enabled": unit_array_enabled(),
        "mode": UNIT_ARRAY_MODE,
        "layout_mode": unit_array_layout_enabled(),
        "min_w_um": UNIT_ARRAY_MIN_W_UM,
        "unit_w_target_um": UNIT_W_UM,
        "max_units": UNIT_ARRAY_MAX_UNITS,
        "devices_planned": 0,
        "devices_layout_replaced": 0,
        "plans": [],
    }
    if not unit_array_enabled():
        return report
    for entry in entries:
        plan = create_unit_array_cell(layout, entry, loaded_gds, fixed_index, fixed_lower_index)
        if plan:
            report["devices_planned"] += 1
            if plan.get("layout_created"):
                report["devices_layout_replaced"] += 1
            report["plans"].append(json_safe(plan))
    return report


def write_unit_array_outputs(out_gds, unit_report):
    if not UNIT_ARRAY_WRITE_FILES:
        return {"written": False}
    json_path = out_gds + ".unit_arrays.json"
    csv_path = out_gds + ".unit_arrays.csv"
    with open(json_path, "w") as f:
        json.dump(json_safe(unit_report), f, indent=2)
    keys = ["inst", "cell", "kind", "original_w_um", "unit_w_um", "l_um", "units", "rows", "cols", "guard_ring", "mode", "layout_created", "wrapper_cell", "unit_cell"]
    with open(csv_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=keys)
        wr.writeheader()
        for p in unit_report.get("plans", []):
            wr.writerow({k: p.get(k, "") for k in keys})
    return {"written": True, "json": json_path, "csv": csv_path, "plans": len(unit_report.get("plans", []))}


def add_unit_array_card(layout, top, entries, unit_report):
    if not UNIT_ARRAY_CARD or not ADD_LABELS or not unit_report.get("plans"):
        return {"unit_array_card": False}
    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs.extend([x0, x1]); ys.extend([y0, y1])
    if not xs:
        return {"unit_array_card": False, "reason": "no extents"}
    x = max(xs) + MAP_OFFSET_UM
    y = min(ys) - float(os.environ.get("SKY_UNIT_CARD_DROP_UM", "30"))
    lines = ["UNIT ARRAY SYNTHESIS", "inst originalW -> units x unitW", "--------------------------------"]
    for p in unit_report.get("plans", [])[:int(os.environ.get("SKY_UNIT_CARD_TOP_N", "16"))]:
        lines.append(f"{p.get('inst')} {p.get('original_w_um'):.4g} -> {p.get('units')} x {p.get('unit_w_um'):.4g}")
    add_text(layout, top, layout.layer(*TEXT_LAYER), "\n".join(lines), x, y, DEVICE_MAP_MAG)
    return {"unit_array_card": True, "x_um": x, "y_um": y, "shown": min(len(unit_report.get("plans", [])), int(os.environ.get("SKY_UNIT_CARD_TOP_N", "16")))}

# -----------------------------------------------------------------------------
# Net-aware rough placement and labels
# -----------------------------------------------------------------------------


def mos_role_nets(device):
    nodes = device.get("nodes") or []
    return {
        "D": nodes[0] if len(nodes) > 0 else "",
        "G": nodes[1] if len(nodes) > 1 else "",
        "S": nodes[2] if len(nodes) > 2 else "",
        "B": nodes[3] if len(nodes) > 3 else "",
    }


def pair_key(device):
    nets = mos_role_nets(device)
    return (nets["D"], nets["G"])


def add_text(layout, top, layer_id, text, x_um, y_um, mag=1.0):
    if not text:
        return
    t = pya.Text(str(text), pya.Trans(dbu_um(layout, x_um), dbu_um(layout, y_um)))
    try:
        t.size = dbu_um(layout, mag)
    except Exception:
        pass
    top.shapes(layer_id).insert(t)


def make_device_label(device, method):
    lines = [device.get("inst", ""), device.get("cell", "")]
    if is_mos(device.get("cell", "")):
        nets = mos_role_nets(device)
        lines.append("D={D} G={G} S={S} B={B}".format(**nets))
        lines.append("W={} L={} nf={} m={}".format(
            device.get("w_um"), device.get("l_um"), device.get("nf"), device.get("m")
        ))
        if device.get("guard_ring"):
            lines.append("guard_ring=YES bulk=guard ring")
    else:
        lines.append("nodes=" + ",".join(device.get("nodes", [])))
        if device.get("w_um") is not None or device.get("l_um") is not None:
            lines.append("W={} L={} m={}".format(device.get("w_um"), device.get("l_um"), device.get("m")))
    lines.append("method=" + str(method))
    return "\n".join(lines)


def bbox_um(entry):
    """Return (width_um, height_um) for a generated/imported KLayout cell."""
    try:
        bbox = entry["cell"].bbox()
        dbu = entry["cell"].layout().dbu
        return max(1.0, bbox.width() * dbu), max(1.0, bbox.height() * dbu)
    except Exception:
        return X_SPACING_UM, Y_SPACING_UM


def entry_bbox_extents_um(entry):
    """Return placed extents (x0,y0,x1,y1) in microns for an entry."""
    x, y = entry.get("position_um", (0.0, 0.0))
    w, h = bbox_um(entry)
    return x, y, x + w, y + h


def pin_roles_for_device(device):
    """Return ordered (pin_role, net_name) pairs for this SPICE instance.

    This preserves the actual SPICE node order.  The roles are convenience labels
    for layout readability; the raw node list is still written to the JSON/CSV
    map so nothing is hidden or altered.
    """
    nodes = list(device.get("nodes") or [])
    cell = device.get("cell", "")
    cat = device_category(cell)

    if is_mos(cell):
        roles = ["D", "G", "S", "B"]
    elif is_bjt(cell):
        # SKY130 primitive BJT netlists are commonly C B E, sometimes C B E S.
        roles = ["C", "B", "E", "S"]
    elif is_diode(cell):
        roles = ["A", "K"]
    elif is_cap(cell) or is_res(cell):
        roles = ["1", "2", "3", "4"]
    else:
        roles = [f"N{i+1}" for i in range(max(len(nodes), 1))]

    out = []
    for i, net in enumerate(nodes):
        role = roles[i] if i < len(roles) else f"N{i+1}"
        out.append((role, net))
    return out


def terminal_anchor_points(device, x_um, y_um, w_um, h_um):
    """Heuristic terminal anchor points for useful top-level labels/guides.

    The generated primitive cells do not expose a uniform pin database.  This
    function intentionally uses device-type-aware anchor locations.  It is not an
    autorouter, but it puts each net name near the side of the device where a
    layout engineer would normally start looking.
    """
    roles = pin_roles_for_device(device)
    cell = device.get("cell", "")

    def pt(role, idx, total):
        # Default: distribute pins along top edge.
        if is_mos(cell):
            locs = {
                "D": (0.12, 0.56),
                "G": (0.50, 0.96),
                "S": (0.88, 0.56),
                "B": (0.50, 0.08),
            }
        elif is_bjt(cell):
            locs = {
                "C": (0.50, 0.94),
                "B": (0.12, 0.52),
                "E": (0.50, 0.08),
                "S": (0.88, 0.52),
            }
        elif is_diode(cell) or is_cap(cell) or is_res(cell):
            locs = {
                "1": (0.12, 0.52),
                "2": (0.88, 0.52),
                "A": (0.12, 0.52),
                "K": (0.88, 0.52),
                "3": (0.50, 0.94),
                "4": (0.50, 0.08),
            }
        else:
            # Standard/IO/unknown cells: distribute along top.
            frac = (idx + 1) / (total + 1)
            return x_um + frac * w_um, y_um + 0.96 * h_um

        fx, fy = locs.get(role, ((idx + 1) / (total + 1), 0.96))
        return x_um + fx * w_um, y_um + fy * h_um

    anchors = []
    total = max(1, len(roles))
    for idx, (role, net) in enumerate(roles):
        ax, ay = pt(role, idx, total)
        anchors.append({
            "inst": device.get("inst", ""),
            "cell": device.get("cell", ""),
            "role": role,
            "net": net,
            "x_um": ax,
            "y_um": ay,
        })
    return anchors


def short_pin_label(role, net):
    if not net:
        return role
    return f"{role}:{net}"


def add_pin_labels_for_entry(layout, top, entry, pin_text_layer):
    if not PIN_LABELS or not ADD_LABELS:
        return []
    d = entry["device"]
    x, y = entry.get("position_um", (0.0, 0.0))
    w, h = bbox_um(entry)
    anchors = terminal_anchor_points(d, x, y, w, h)
    for a in anchors:
        add_text(layout, top, pin_text_layer, short_pin_label(a["role"], a["net"]), a["x_um"], a["y_um"], PIN_LABEL_MAG)
    entry["pin_anchors"] = anchors
    return anchors



def local_stub_vector_for_role(device, role, ax, ay, w_um, h_um):
    """Return a short outward vector for a movable local terminal guide stub."""
    cell = device.get("cell", "")
    role = str(role)
    # Default direction from relative location to nearest outside edge.
    cx, cy = max(w_um, 1.0) / 2.0, max(h_um, 1.0) / 2.0

    if is_mos(cell):
        vecs = {
            "D": (-1.0, 0.0),
            "G": (0.0, 1.0),
            "S": (1.0, 0.0),
            "B": (0.0, -1.0),
        }
    elif is_bjt(cell):
        vecs = {
            "C": (0.0, 1.0),
            "B": (-1.0, 0.0),
            "E": (0.0, -1.0),
            "S": (1.0, 0.0),
        }
    elif is_diode(cell) or is_cap(cell) or is_res(cell):
        vecs = {
            "1": (-1.0, 0.0),
            "2": (1.0, 0.0),
            "A": (-1.0, 0.0),
            "K": (1.0, 0.0),
            "3": (0.0, 1.0),
            "4": (0.0, -1.0),
        }
    else:
        vecs = {}

    if role in vecs:
        return vecs[role]

    # Fallback: push away from cell center.
    dx = ax - cx
    dy = ay - cy
    if abs(dx) >= abs(dy):
        return (1.0 if dx >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy >= 0 else -1.0)


def add_local_pin_stubs_for_wrapper(layout, wrapper, device, local_anchors, w_um, h_um):
    """Draw short movable local guide stubs inside the annotated wrapper cell."""
    if not LOCAL_GUIDES_ENABLED or not ADD_LABELS:
        return {"local_pin_stubs": False}
    stub_layer = layout.layer(*LOCAL_STUB_LAYER)
    text_layer = layout.layer(*GUIDE_TEXT_LAYER)
    width = max(1, dbu_um(layout, LOCAL_STUB_WIDTH_UM))
    count = 0
    for a in local_anchors:
        ax = float(a.get("x_um", 0.0))
        ay = float(a.get("y_um", 0.0))
        role = a.get("role", "")
        net = a.get("net", "")
        vx, vy = local_stub_vector_for_role(device, role, ax, ay, w_um, h_um)
        ex = ax + vx * LOCAL_STUB_LEN_UM
        ey = ay + vy * LOCAL_STUB_LEN_UM
        try:
            wrapper.shapes(stub_layer).insert(pya.Path([
                pya.Point(dbu_um(layout, ax), dbu_um(layout, ay)),
                pya.Point(dbu_um(layout, ex), dbu_um(layout, ey)),
            ], width))
            count += 1
        except Exception:
            pass
        if LOCAL_STUB_TEXT_AT_END and net:
            add_text(layout, wrapper, text_layer, str(net), ex, ey, PIN_LABEL_MAG)
    return {"local_pin_stubs": True, "count": count, "layer": LOCAL_STUB_LAYER}


def make_annotated_device_wrapper(layout, entry):
    """Replace entry['cell'] by a wrapper that moves labels/stubs with the device.

    Top-level cross-device guide lines are static GDS shapes and cannot follow a
    moved instance.  This wrapper makes the practical annotations that matter
    most--pin names, net names, and local route start stubs--part of the device
    instance itself.  So dragging the wrapper in KLayout keeps the annotations
    attached.
    """
    if not MOVABLE_DEVICE_WRAPPERS:
        return entry
    if not ADD_LABELS and not LOCAL_GUIDES_ENABLED:
        return entry

    base = entry.get("cell")
    if base is None:
        return entry
    device = entry.get("device", {})
    method = entry.get("method", "")
    bbox = base.bbox()
    dbu = layout.dbu
    w_um = max(1.0, bbox.width() * dbu)
    h_um = max(1.0, bbox.height() * dbu)

    wrap_name = sanitize_cell_name("ANN_%s_%s" % (device.get("inst", "X"), base.name))
    existing = layout.cell(wrap_name)
    if existing is not None:
        wrapper = existing
    else:
        wrapper = layout.create_cell(wrap_name)
        # Put the base device bbox bottom-left at wrapper local origin.
        wrapper.insert(pya.CellInstArray(base.cell_index(), pya.Trans(-bbox.left, -bbox.bottom)))

        text_layer = layout.layer(*TEXT_LAYER)
        pin_layer = layout.layer(*PIN_TEXT_LAYER)
        if ADD_LABELS:
            add_text(layout, wrapper, text_layer, device.get("inst", ""), 0.0, -1.4, DEVICE_LABEL_MAG)
            if device.get("match_group_id"):
                add_text(layout, wrapper, text_layer, "MG:" + str(device.get("match_group_id")), 0.0, -2.7, DEVICE_LABEL_MAG * 0.9)

        local_anchors = terminal_anchor_points(device, 0.0, 0.0, w_um, h_um)
        if ADD_LABELS and PIN_LABELS:
            for a in local_anchors:
                add_text(layout, wrapper, pin_layer, short_pin_label(a["role"], a["net"]), a["x_um"], a["y_um"], PIN_LABEL_MAG)
        stub_report = add_local_pin_stubs_for_wrapper(layout, wrapper, device, local_anchors, w_um, h_um)
        entry["local_stub_report"] = stub_report

    # Preserve original generated/imported cell information for reports.
    entry["base_cell"] = base
    entry["base_cell_name"] = base.name
    entry["base_bbox_um"] = {"w_um": w_um, "h_um": h_um}
    entry["cell"] = wrapper
    entry["annotation_wrapper"] = True
    entry["local_pin_anchors"] = terminal_anchor_points(device, 0.0, 0.0, w_um, h_um)
    return entry


def absolute_pin_anchors_for_entry(entry):
    """Convert wrapper-local anchors to absolute top-level coordinates."""
    x, y = entry.get("position_um", (0.0, 0.0))
    anchors = []
    for a in entry.get("local_pin_anchors", []):
        aa = dict(a)
        aa["x_um"] = x + float(a.get("x_um", 0.0))
        aa["y_um"] = y + float(a.get("y_um", 0.0))
        aa["movable_with_device"] = bool(entry.get("annotation_wrapper"))
        anchors.append(aa)
    entry["pin_anchors"] = anchors
    return anchors

def device_map_line(entry):
    d = entry["device"]
    roles = pin_roles_for_device(d)
    role_txt = " ".join(f"{r}={n}" for r, n in roles)
    dim = ""
    if d.get("w_um") is not None or d.get("l_um") is not None:
        dim = f" W={d.get('w_um')} L={d.get('l_um')} nf={d.get('nf')} m={d.get('m')}"
    gr = " GR" if d.get("guard_ring") else ""
    return f"{d.get('inst')} {device_category(d.get('cell'))}{gr} {role_txt}{dim}"


def add_device_map_card(layout, top, entries):
    """Place a compact device-to-net map away from actual devices."""
    if not DEVICE_MAP_CARD or not ADD_LABELS or not entries:
        return {"device_map_card": False}

    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs.extend([x0, x1])
        ys.extend([y0, y1])
    if not xs:
        return {"device_map_card": False, "reason": "no extents"}

    map_layer = layout.layer(*TEXT_LAYER)
    if MAP_SIDE in ("below", "bottom"):
        x_start = min(xs)
        y_start = min(ys) - MAP_OFFSET_UM
    else:
        x_start = max(xs) + MAP_OFFSET_UM
        y_start = max(ys)

    header = [
        "DEVICE NET MAP (SPICE ORDER PRESERVED)",
        "MOS=D/G/S/B  BJT=C/B/E  diode=A/K  cap/res=1/2",
        "--------------------------------------------",
    ]
    lines = header + [device_map_line(e) for e in entries]

    # Split large maps into columns so the text remains readable in KLayout.
    lines_per_col = int(os.environ.get("SKY_MAP_LINES_PER_COL", "24"))
    line_pitch = float(os.environ.get("SKY_MAP_LINE_PITCH_UM", "1.45"))
    col_pitch = float(os.environ.get("SKY_MAP_COL_PITCH_UM", "82"))

    for col, i in enumerate(range(0, len(lines), lines_per_col)):
        block = "\n".join(lines[i:i + lines_per_col])
        add_text(layout, top, map_layer, block, x_start + col * col_pitch, y_start, DEVICE_MAP_MAG)

    return {
        "device_map_card": True,
        "x_um": x_start,
        "y_um": y_start,
        "lines": len(lines),
        "lines_per_col": lines_per_col,
        "side": MAP_SIDE,
        "offset_um": MAP_OFFSET_UM,
    }


def is_power_like_net(net):
    n = str(net).lower()
    return n in ("vdd", "vdda", "vccd1", "vpwr", "vss", "vssa", "vssd1", "vgnd", "gnd", "0")

def parse_name_set(raw):
    """Parse comma/semicolon/space separated net names."""
    if raw is None:
        return set()
    return {x.strip() for x in re.split(r"[,;\s]+", str(raw)) if x.strip()}


def parse_net_layer_map(raw):
    """Parse SKY_GUIDE_NET_LAYER_MAP='net1=255,12;net2=255,13'."""
    out = {}
    if not raw:
        return out
    for item in re.split(r"[;]+", str(raw)):
        if "=" not in item:
            continue
        name, val = item.split("=", 1)
        name = name.strip()
        parts = [p.strip() for p in val.split(",") if p.strip()]
        if name and len(parts) == 2:
            try:
                out[name] = (int(parts[0]), int(parts[1]))
            except Exception:
                pass
    return out


def guide_layer_for_net(net):
    """Return a layer/datatype tuple for a net guide.

    GDS itself does not store display colors, but using different datatypes lets
    KLayout display different guide classes distinctly through the SKY130 layer
    properties or through manual layer styling.
    """
    explicit = parse_net_layer_map(GUIDE_NET_LAYER_MAP_RAW)
    if net in explicit:
        return explicit[net]
    if not GUIDE_PER_NET_LAYERS:
        return GUIDE_LAYER
    base_l, base_d = GUIDE_LAYER
    # Deterministic hash, unlike Python hash() which is salted per process.
    delta = zlib.crc32(str(net).encode("utf-8")) % GUIDE_LAYER_SPAN
    return (base_l, base_d + int(delta))


def collect_net_anchors(entries):
    net_to_anchors = {}
    for e in entries:
        for a in e.get("pin_anchors", []):
            net = a.get("net")
            if not net:
                continue
            net_to_anchors.setdefault(net, []).append(a)
    return net_to_anchors


def build_net_statistics(entries):
    """Build routing-priority statistics from terminal anchors."""
    net_to_anchors = collect_net_anchors(entries)
    stats = []
    for net, anchors in net_to_anchors.items():
        xs = [float(a.get("x_um", 0.0)) for a in anchors]
        ys = [float(a.get("y_um", 0.0)) for a in anchors]
        insts = sorted({a.get("inst", "") for a in anchors if a.get("inst")})
        roles = sorted({a.get("role", "") for a in anchors if a.get("role")})
        hpwl = (max(xs) - min(xs) if xs else 0.0) + (max(ys) - min(ys) if ys else 0.0)
        power = is_power_like_net(net)
        # Priority: fanout first, then approximate wire length.  Power nets are
        # kept in the report but excluded from guide lines unless explicitly enabled.
        score = (len(anchors) * 1000.0) + hpwl + (250.0 if not power else 0.0)
        stats.append({
            "net": net,
            "pin_count": len(anchors),
            "inst_count": len(insts),
            "instances": insts,
            "roles": roles,
            "hpwl_um": hpwl,
            "x_span_um": max(xs) - min(xs) if xs else 0.0,
            "y_span_um": max(ys) - min(ys) if ys else 0.0,
            "is_power": power,
            "guide_layer": guide_layer_for_net(net),
            "priority_score": score,
        })
    stats.sort(key=lambda r: (-r["priority_score"], str(r["net"])))
    return stats


def should_draw_guide_for_net(net, anchors, ranked_nets):
    if not NET_GUIDES:
        return False
    selected = parse_name_set(GUIDE_NETS_RAW)
    excluded = parse_name_set(GUIDE_EXCLUDE_NETS_RAW)

    if str(net) in excluded:
        return False
    if is_power_like_net(net) and not GUIDE_INCLUDE_POWER:
        return False
    if len(anchors) < GUIDE_MIN_PINS:
        return False

    mode = GUIDE_MODE
    if selected and mode not in ("all", "full"):
        # If the user supplied SKY_GUIDE_NETS, treat default/important as selected.
        return str(net) in selected
    if mode in ("selected", "select", "only"):
        return str(net) in selected
    if mode in ("all", "full", "debug"):
        return True
    # important/default: highest priority nets only.
    return str(net) in set(ranked_nets[:GUIDE_TOP_N])


def detect_and_tag_matched_groups(entries):
    """Detect useful analog matched-device groups.

    This does not modify the SPICE connectivity.  It tags devices for reporting,
    KLayout properties, and adjacent placement.  It looks for common analog
    patterns that help the user inspect current mirrors and differential pairs.
    """
    if not MATCH_GROUPS:
        return {"match_groups_enabled": False, "groups": []}

    def dim_key(d):
        return (
            d.get("cell"), mos_kind(d.get("cell", "")),
            round(float(d.get("w_um") or 0.0), 6),
            round(float(d.get("l_um") or 0.0), 6),
            int(d.get("nf") or 1),
        )

    mos_entries = [e for e in entries if is_mos(e["device"].get("cell", ""))]
    groups = []
    gid = 1

    # Differential-pair candidates: same type/dim, common source and bulk,
    # different gates.  Drains may or may not be different in early schematics.
    buckets = {}
    for e in mos_entries:
        d = e["device"]
        n = mos_role_nets(d)
        key = dim_key(d) + (n.get("S"), n.get("B"))
        buckets.setdefault(key, []).append(e)
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        gates = {mos_role_nets(e["device"]).get("G") for e in items}
        if len(gates) >= 2:
            group_id = f"DIFF{gid}"
            gid += 1
            insts = []
            for e in items:
                e["match_group_id"] = group_id
                e["match_group_type"] = "differential_pair_candidate"
                e["device"]["match_group_id"] = group_id
                e["device"]["match_group_type"] = "differential_pair_candidate"
                insts.append(e["device"].get("inst"))
            groups.append({"id": group_id, "type": "differential_pair_candidate", "instances": insts, "reason": "same model/W/L/nf with common S/B and different gates"})

    # Current-mirror candidates: same type/dim, common source/bulk, either same
    # gate or one diode-connected device feeding another gate.
    mirror_buckets = {}
    for e in mos_entries:
        d = e["device"]
        n = mos_role_nets(d)
        key = dim_key(d) + (n.get("S"), n.get("B"), n.get("G"))
        mirror_buckets.setdefault(key, []).append(e)
    for key, items in mirror_buckets.items():
        if len(items) < 2:
            continue
        group_id = f"MIR{gid}"
        gid += 1
        insts = []
        for e in items:
            # Do not overwrite differential-pair tags unless no tag exists.
            e.setdefault("match_group_id", group_id)
            e.setdefault("match_group_type", "current_mirror_candidate")
            e["device"].setdefault("match_group_id", group_id)
            e["device"].setdefault("match_group_type", "current_mirror_candidate")
            insts.append(e["device"].get("inst"))
        groups.append({"id": group_id, "type": "current_mirror_candidate", "instances": insts, "reason": "same model/W/L/nf with common S/B/G"})

    # Generic matched arrays: same primitive and dimensions.  Useful for unit
    # devices that are not obvious mirrors/diff pairs yet.
    dim_buckets = {}
    for e in mos_entries:
        dim_buckets.setdefault(dim_key(e["device"]), []).append(e)
    for key, items in dim_buckets.items():
        if len(items) < 2:
            continue
        # Only create a generic group if at least one item lacks a more specific group.
        untagged = [e for e in items if not e.get("match_group_id")]
        if len(untagged) < 2:
            continue
        group_id = f"MAT{gid}"
        gid += 1
        insts = []
        for e in untagged:
            e["match_group_id"] = group_id
            e["match_group_type"] = "matched_dimension_candidate"
            e["device"]["match_group_id"] = group_id
            e["device"]["match_group_type"] = "matched_dimension_candidate"
            insts.append(e["device"].get("inst"))
        groups.append({"id": group_id, "type": "matched_dimension_candidate", "instances": insts, "reason": "same model/W/L/nf"})

    return {"match_groups_enabled": True, "groups": groups, "count": len(groups)}


def add_routing_priority_card(layout, top, entries, net_stats, map_report=None):
    if not ROUTING_PRIORITY_CARD or not ADD_LABELS or not net_stats:
        return {"routing_priority_card": False}
    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs.extend([x0, x1])
        ys.extend([y0, y1])
    if not xs:
        return {"routing_priority_card": False, "reason": "no extents"}
    layer = layout.layer(*TEXT_LAYER)
    # Put report next to the device map, not inside the device area.
    x = max(xs) + MAP_OFFSET_UM
    y = max(ys) - float(os.environ.get("SKY_PRIORITY_CARD_DROP_UM", "45"))
    if map_report and map_report.get("device_map_card") and MAP_SIDE == "right":
        y = map_report.get("y_um", y) - float(os.environ.get("SKY_PRIORITY_CARD_DROP_UM", "45"))
    top_n = int(os.environ.get("SKY_PRIORITY_CARD_TOP_N", "12"))
    lines = ["ROUTING PRIORITY", "net pins hpwl_um guide_layer power", "--------------------------------"]
    for r in net_stats[:top_n]:
        layer_txt = "%s/%s" % tuple(r.get("guide_layer", GUIDE_LAYER))
        lines.append(f"{r['net']} {r['pin_count']} {r['hpwl_um']:.1f} {layer_txt} {'PWR' if r['is_power'] else ''}")
    add_text(layout, top, layer, "\n".join(lines), x, y, DEVICE_MAP_MAG)
    return {"routing_priority_card": True, "x_um": x, "y_um": y, "nets_shown": min(top_n, len(net_stats))}


def add_match_group_card(layout, top, entries, match_report, map_report=None):
    if not MATCH_GROUP_CARD or not ADD_LABELS or not match_report.get("groups"):
        return {"match_group_card": False}
    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs.extend([x0, x1])
        ys.extend([y0, y1])
    if not xs:
        return {"match_group_card": False, "reason": "no extents"}
    layer = layout.layer(*TEXT_LAYER)
    x = max(xs) + MAP_OFFSET_UM
    y = max(ys) - float(os.environ.get("SKY_MATCH_CARD_DROP_UM", "85"))
    lines = ["MATCHED DEVICE GROUPS", "--------------------------------"]
    for g in match_report.get("groups", [])[:int(os.environ.get("SKY_MATCH_CARD_TOP_N", "12"))]:
        lines.append(f"{g['id']} {g['type']}: {','.join(g['instances'])}")
    add_text(layout, top, layer, "\n".join(lines), x, y, DEVICE_MAP_MAG)
    return {"match_group_card": True, "x_um": x, "y_um": y, "groups_shown": min(len(match_report.get("groups", [])), int(os.environ.get("SKY_MATCH_CARD_TOP_N", "12")))}



def add_net_guides(layout, top, entries, net_stats=None):
    """Draw non-fab guide lines between same-net terminal labels.

    Guide visibility is controlled by SKY_ROUTE_GUIDES / SKY_GUIDE_MODE:
      off       : no guide lines
      important : top nets only, power excluded unless SKY_GUIDE_INCLUDE_POWER=1
      selected  : only SKY_GUIDE_NETS
      all       : every non-excluded net
    """
    if not TOP_GUIDES_ENABLED:
        return {"net_guides": False, "mode": GUIDE_MODE, "guide_style": GUIDE_STYLE, "reason": "top-level static guide lines disabled; using movable local stubs"}

    net_to_anchors = collect_net_anchors(entries)
    net_stats = net_stats or build_net_statistics(entries)
    ranked_nets = [r["net"] for r in net_stats if not (r.get("is_power") and not GUIDE_INCLUDE_POWER)]

    drawn = 0
    labelled = 0
    skipped = 0
    width = max(1, dbu_um(layout, GUIDE_WIDTH_UM))
    max_per_net = int(os.environ.get("SKY_GUIDE_MAX_PINS_PER_NET", "12"))
    drawn_nets = []
    layers_used = {}

    for net, anchors in sorted(net_to_anchors.items()):
        if len(anchors) < 2 or len(anchors) > max_per_net:
            skipped += 1
            continue
        if not should_draw_guide_for_net(net, anchors, ranked_nets):
            skipped += 1
            continue

        layer_tuple = guide_layer_for_net(net)
        guide_layer = layout.layer(*layer_tuple)
        guide_text_layer = layout.layer(*GUIDE_TEXT_LAYER)
        layers_used[str(net)] = layer_tuple

        # Star from the nearest-to-centroid anchor to reduce extreme line length.
        cx = sum(float(a["x_um"]) for a in anchors) / len(anchors)
        cy = sum(float(a["y_um"]) for a in anchors) / len(anchors)
        root = min(anchors, key=lambda a: (float(a["x_um"]) - cx) ** 2 + (float(a["y_um"]) - cy) ** 2)
        rpoint = pya.Point(dbu_um(layout, root["x_um"]), dbu_um(layout, root["y_um"]))
        xs = [root["x_um"]]
        ys = [root["y_um"]]
        for a in anchors:
            if a is root:
                continue
            apoint = pya.Point(dbu_um(layout, a["x_um"]), dbu_um(layout, a["y_um"]))
            try:
                top.shapes(guide_layer).insert(pya.Path([rpoint, apoint], width))
                drawn += 1
            except Exception:
                pass
            xs.append(a["x_um"])
            ys.append(a["y_um"])
        add_text(layout, top, guide_text_layer, f"net:{net}", sum(xs)/len(xs), sum(ys)/len(ys), 0.55)
        labelled += 1
        drawn_nets.append(net)

    return {
        "net_guides": True,
        "mode": GUIDE_MODE,
        "segments": drawn,
        "labelled_nets": labelled,
        "drawn_nets": drawn_nets,
        "skipped_nets": skipped,
        "base_layer": GUIDE_LAYER,
        "per_net_layers": GUIDE_PER_NET_LAYERS,
        "layers_used": layers_used,
        "exclude_nets": sorted(parse_name_set(GUIDE_EXCLUDE_NETS_RAW)),
        "include_power": GUIDE_INCLUDE_POWER,
    }



def net_is_internal_signal(net):
    if not net:
        return False
    return not is_power_like_net(net)


def mos_dim_signature(device):
    return (
        device.get("cell"), mos_kind(device.get("cell", "")),
        round(float(device.get("w_um") or 0.0), 6),
        round(float(device.get("l_um") or 0.0), 6),
        int(device.get("nf") or 1),
    )


def mos_source_bulk_key(device):
    n = mos_role_nets(device)
    return (n.get("S", ""), n.get("B", ""))


def tag_topology(entry, topo_class=None, topo_role=None, note=None):
    d = entry.get("device", {})
    if topo_class and not d.get("topology_class"):
        d["topology_class"] = topo_class
        entry["topology_class"] = topo_class
    if topo_role and not d.get("topology_role"):
        d["topology_role"] = topo_role
        entry["topology_role"] = topo_role
    if note:
        d.setdefault("topology_notes", []).append(note)
        entry.setdefault("topology_notes", []).append(note)


def analyze_circuit_topology(entries, metadata=None):
    """Infer layout-relevant circuit topology from parsed SPICE connectivity.

    This is a layout-assist pass, not LVS. It detects patterns that matter to
    analog/digital layout: diode-connected MOS, current mirrors, diff-pairs,
    stacked/cascode chains, CMOS inverter-like pairs, BJT branches, and likely
    sensitive/high-impedance nodes.
    """
    if not TOPOLOGY_ANALYSIS or not ANALOGSENSE:
        return {"topology_analysis": False, "devices": [], "groups": [], "nets": {}}
    metadata = metadata or {}
    mos_entries = [e for e in entries if is_mos(e["device"].get("cell", ""))]
    bjt_entries = [e for e in entries if is_bjt(e["device"].get("cell", ""))]
    groups = []
    gid = 1
    net_roles = {}
    for e in entries:
        d = e["device"]
        for role, net in pin_roles_for_device(d):
            net_roles.setdefault(net, []).append({
                "inst": d.get("inst"), "role": role,
                "category": device_category(d.get("cell", "")),
                "cell": d.get("cell", ""),
            })

    diode_mos = []
    for e in mos_entries:
        n = mos_role_nets(e["device"])
        if n.get("D") and n.get("D") == n.get("G"):
            tag_topology(e, "diode_connected_mos", "reference_or_bias_device", "D and G are tied")
            diode_mos.append(e["device"].get("inst"))
    if diode_mos:
        groups.append({"id": f"TOPO{gid}", "type": "diode_connected_mos", "instances": diode_mos, "reason": "MOS drain and gate are tied"})
        gid += 1

    by_dim_sb = {}
    for e in mos_entries:
        d = e["device"]
        by_dim_sb.setdefault((mos_dim_signature(d),) + mos_source_bulk_key(d), []).append(e)
    for key, items in by_dim_sb.items():
        diode_refs = []
        for e in items:
            n = mos_role_nets(e["device"])
            if n.get("D") and n.get("D") == n.get("G"):
                diode_refs.append(e)
        for ref in diode_refs:
            rn = mos_role_nets(ref["device"])
            outs = [e for e in items if e is not ref and mos_role_nets(e["device"]).get("G") == rn.get("G")]
            if outs:
                group_id = f"MIRR{gid}"; gid += 1
                insts = [ref["device"].get("inst")] + [e["device"].get("inst") for e in outs]
                for e in [ref] + outs:
                    e["device"].setdefault("topology_class", "current_mirror")
                    e["device"].setdefault("topology_role", "mirror_reference" if e is ref else "mirror_output")
                    e["device"].setdefault("topology_group", group_id)
                    e.setdefault("topology_group", group_id)
                groups.append({"id": group_id, "type": "current_mirror", "reference": ref["device"].get("inst"), "outputs": [e["device"].get("inst") for e in outs], "instances": insts, "gate_net": rn.get("G"), "reason": "diode-connected MOS gate drives same-dimension devices with common source/body"})

    # Ratioed current mirrors: same source/body/gate and same MOS type, but W/L/nf may differ.
    # This catches useful analog cases such as a 1x diode reference driving 15x/64x outputs.
    by_kind_sb_gate = {}
    for e in mos_entries:
        d = e["device"]
        n = mos_role_nets(d)
        if not n.get("G"):
            continue
        kind_key = (mos_kind(d.get("cell", "")), n.get("S"), n.get("B"), n.get("G"))
        by_kind_sb_gate.setdefault(kind_key, []).append(e)
    for key, items in by_kind_sb_gate.items():
        diode_refs = [e for e in items if mos_role_nets(e["device"]).get("D") == mos_role_nets(e["device"]).get("G")]
        for ref in diode_refs:
            rn = mos_role_nets(ref["device"])
            outs = [e for e in items if e is not ref and mos_role_nets(e["device"]).get("G") == rn.get("G")]
            if not outs:
                continue
            # Reuse an existing mirror group if this reference already created one.
            existing = None
            for g in groups:
                if g.get("type") in ("current_mirror", "ratioed_current_mirror") and g.get("reference") == ref["device"].get("inst") and g.get("gate_net") == rn.get("G"):
                    existing = g
                    break
            if existing is None:
                group_id = f"RMIRR{gid}"; gid += 1
                insts = [ref["device"].get("inst")]
                outputs = []
                existing = {"id": group_id, "type": "ratioed_current_mirror", "reference": ref["device"].get("inst"), "outputs": outputs, "instances": insts, "gate_net": rn.get("G"), "reason": "diode-connected MOS gate drives same-type devices with common source/body; dimensions may be ratioed"}
                groups.append(existing)
            else:
                group_id = existing.get("id")
                if "ratioed" not in existing.get("reason", "") and any(mos_dim_signature(e["device"]) != mos_dim_signature(ref["device"]) for e in outs):
                    existing["reason"] = existing.get("reason", "") + "; includes ratioed-width outputs"
            for e in [ref] + outs:
                if e["device"].get("inst") not in existing.setdefault("instances", []):
                    existing["instances"].append(e["device"].get("inst"))
                if e is not ref and e["device"].get("inst") not in existing.setdefault("outputs", []):
                    existing["outputs"].append(e["device"].get("inst"))
                e["device"].setdefault("topology_class", "current_mirror")
                e["device"].setdefault("topology_role", "mirror_reference" if e is ref else "mirror_output")
                e["device"].setdefault("topology_group", group_id)
                e.setdefault("topology_group", group_id)

    for key, items in by_dim_sb.items():
        if len(items) < 2:
            continue
        non_diode = [e for e in items if mos_role_nets(e["device"]).get("D") != mos_role_nets(e["device"]).get("G")]
        gates = sorted({mos_role_nets(e["device"]).get("G") for e in non_diode if mos_role_nets(e["device"]).get("G")})
        if len(non_diode) >= 2 and len(gates) >= 2:
            group_id = f"DIFFTOPO{gid}"; gid += 1
            insts = [e["device"].get("inst") for e in non_diode]
            for e in non_diode:
                e["device"].setdefault("topology_class", "differential_pair")
                e["device"].setdefault("topology_role", "input_pair_device")
                e["device"].setdefault("topology_group", group_id)
            groups.append({"id": group_id, "type": "differential_pair_candidate", "instances": insts, "input_gate_nets": gates, "reason": "same type/dim with common source/body and different gates"})

    pfets = [e for e in mos_entries if mos_kind(e["device"].get("cell", "")) == "pfet"]
    nfets = [e for e in mos_entries if mos_kind(e["device"].get("cell", "")) == "nfet"]
    for p in pfets:
        pn = mos_role_nets(p["device"])
        for n in nfets:
            nn = mos_role_nets(n["device"])
            if pn.get("G") and pn.get("G") == nn.get("G") and pn.get("D") and pn.get("D") == nn.get("D"):
                group_id = f"CMOS{gid}"; gid += 1
                for e, role in [(p, "pullup"), (n, "pulldown")]:
                    e["device"].setdefault("topology_class", "digital_cmos_gate")
                    e["device"].setdefault("topology_role", role)
                    e["device"].setdefault("topology_group", group_id)
                groups.append({"id": group_id, "type": "digital_cmos_gate", "instances": [p["device"].get("inst"), n["device"].get("inst")], "input": pn.get("G"), "output": pn.get("D"), "reason": "PFET/NFET share gate and drain/output net"})

    seen_stack_pairs = set()
    for i, a in enumerate(mos_entries):
        an = mos_role_nets(a["device"])
        for b in mos_entries[i + 1:]:
            if mos_kind(a["device"].get("cell", "")) != mos_kind(b["device"].get("cell", "")):
                continue
            bn = mos_role_nets(b["device"])
            internal = ({an.get("D"), an.get("S")} & {bn.get("D"), bn.get("S")}) - {"", None}
            internal = {net for net in internal if net_is_internal_signal(net)}
            if internal:
                key = tuple(sorted([a["device"].get("inst"), b["device"].get("inst")]))
                if key in seen_stack_pairs:
                    continue
                seen_stack_pairs.add(key)
                for e in (a, b):
                    e["device"].setdefault("topology_class", "cascode_or_stack")
                    e["device"].setdefault("topology_role", "stacked_device")
                groups.append({"id": f"STACK{gid}", "type": "cascode_or_stack_candidate", "instances": [a["device"].get("inst"), b["device"].get("inst")], "shared_internal_nets": sorted(internal), "reason": "same-type MOS devices share an internal source/drain net"})
                gid += 1

    if bjt_entries:
        insts = []
        for e in bjt_entries:
            tag_topology(e, "bjt_branch", "vertical_bjt_or_bipolar_device", "BJT primitive/fixed GDS device")
            insts.append(e["device"].get("inst"))
        groups.append({"id": f"BJT{gid}", "type": "bjt_branch", "instances": insts, "reason": "BJT devices should usually be kept close to bias/reference devices in analog layouts"})
        gid += 1

    net_summary = {}
    for net, pins in net_roles.items():
        roles = {p["role"] for p in pins}
        cats = {p["category"] for p in pins}
        gate_count = sum(1 for p in pins if p["role"] == "G")
        drain_count = sum(1 for p in pins if p["role"] in ("D", "C", "A", "1"))
        if is_power_like_net(net):
            kind = "power"
        elif gate_count >= 2 and drain_count == 0:
            kind = "high_impedance_bias_or_gate_bus"
        elif gate_count >= 1 and drain_count >= 1:
            kind = "bias_or_feedback_sensitive"
        elif len(pins) >= 4:
            kind = "high_fanout_signal"
        else:
            kind = "local_signal"
        net_summary[net] = {"classification": kind, "pin_count": len(pins), "roles": sorted(roles), "categories": sorted(cats), "pins": pins}

    devices = []
    for e in entries:
        d = e["device"]
        devices.append({"inst": d.get("inst"), "cell": d.get("cell"), "category": device_category(d.get("cell", "")), "topology_class": d.get("topology_class", "unclassified"), "topology_role": d.get("topology_role", ""), "topology_group": d.get("topology_group", ""), "nodes": d.get("nodes", [])})
    return {"topology_analysis": True, "layout_style": LAYOUT_STYLE, "devices": devices, "groups": groups, "nets": net_summary}


def enhance_routing_priority(entries, net_stats, topology_report=None, match_report=None):
    topology_report = topology_report or {}
    net_classes = topology_report.get("nets", {})
    gate_nets = set()
    mirror_nets = set()
    for e in entries:
        d = e["device"]
        if is_mos(d.get("cell", "")):
            n = mos_role_nets(d)
            if n.get("G"):
                gate_nets.add(n.get("G"))
            if d.get("topology_class") in ("current_mirror", "diode_connected_mos") and n.get("G"):
                mirror_nets.add(n.get("G"))
    out = []
    for r in net_stats:
        rr = dict(r)
        net = rr.get("net")
        nclass = net_classes.get(net, {}).get("classification", "")
        recs = []
        criticality = "normal"
        score_boost = 0.0
        if rr.get("is_power"):
            recs.append("Use wide metal and short/body-tap-friendly route; keep rail resistance low.")
            criticality = "power"
        if net in mirror_nets:
            recs.append("Mirror/bias gate net: keep short, symmetric, and away from noisy switching routes.")
            criticality = "high"
            score_boost += 800
        elif net in gate_nets and not rr.get("is_power"):
            recs.append("Gate net: avoid unnecessary length; consider shielding if high impedance.")
            criticality = "medium"
            score_boost += 350
        if nclass in ("high_impedance_bias_or_gate_bus", "bias_or_feedback_sensitive"):
            recs.append("Sensitive analog node: keep compact, minimize coupling, avoid crossing noisy rails.")
            criticality = "high"
            score_boost += 600
        if float(rr.get("hpwl_um") or 0.0) > SENSITIVE_HPWL_UM and not rr.get("is_power"):
            recs.append("Large estimated HPWL: place related devices closer or route with shielding/upper metal.")
            score_boost += 250
        if not recs:
            recs.append("Normal local interconnect; route after priority/sensitive nets.")
        rr["net_class"] = nclass
        rr["criticality"] = criticality
        rr["recommendation"] = " ".join(recs)
        rr["priority_score"] = float(rr.get("priority_score") or 0.0) + score_boost
        out.append(rr)
    out.sort(key=lambda r: (-float(r.get("priority_score") or 0.0), str(r.get("net"))))
    return out


def build_layout_warnings(entries, topology_report=None, match_report=None, metadata=None):
    if not LAYOUT_WARNINGS or not ANALOGSENSE:
        return {"layout_warnings": False, "warnings": []}
    warnings = []
    def add(severity, inst, category, message, recommendation=""):
        warnings.append({"severity": severity, "inst": inst or "", "category": category, "message": message, "recommendation": recommendation})
    for e in entries:
        d = e["device"]
        cell = d.get("cell", "")
        inst = d.get("inst", "")
        if is_mos(cell):
            n = mos_role_nets(d)
            kind = mos_kind(cell)
            w = float(d.get("w_um") or 0.0)
            nf = int(d.get("nf") or 1)
            body = n.get("B", "")
            source = n.get("S", "")
            if w >= LARGE_MOS_W_UM and nf <= 1:
                add("medium", inst, "large_single_finger_mos", f"Large MOS W={w:g}um with nf={nf}.", "Consider multi-finger/interdigitated placement or explicit nf to improve gate resistance, diffusion sharing, and matching.")
            if kind == "nfet" and body and str(body).lower() not in ("0", "gnd", "vss", "vssa", "vssd1", "vgnd"):
                add("high", inst, "nmos_body_net", f"NMOS body is tied to {body}, not a usual ground/substrate net.", "Verify intentional body bias/deep-nwell isolation; otherwise tie body/substrate to quiet ground with close taps.")
            if kind == "pfet" and body and str(body).lower() not in ("vdd", "vdda", "vccd1", "vpwr"):
                add("high", inst, "pmos_body_net", f"PMOS body is tied to {body}, not a usual VDD/nwell net.", "Verify well grouping and body connection; devices sharing body should share same nwell/tap strategy.")
            if not body:
                add("medium", inst, "missing_body_terminal", "MOS body terminal was not found in parsed SPICE nodes.", "Check netlist model order and confirm body connection before layout/LVS.")
            if source and body and source != body and d.get("topology_class") in ("current_mirror", "differential_pair", "diode_connected_mos"):
                add("low", inst, "source_body_difference", f"Source={source} and body={body} differ in an analog-sensitive topology.", "Check body effect impact and matching; keep body ties symmetric if intentional.")
            if d.get("topology_class") in ("current_mirror", "differential_pair") and not d.get("guard_ring"):
                add("medium", inst, "analog_sensitive_no_guard_ring", f"{d.get('topology_class')} device has no selected guard ring.", "Consider guard ring/taps for substrate-noise sensitive analog devices.")
            if d.get("topology_class") == "diode_connected_mos":
                add("info", inst, "diode_connected_mos", "MOS is diode-connected D=G.", "Keep reference/bias diode MOS close to its mirror outputs and route gate/drain net compactly.")
        elif is_bjt(cell):
            add("info", inst, "bjt_branch", "BJT/fixed primitive detected.", "Keep BJT/reference branch close, avoid thermal gradients, and consider common-centroid/thermal symmetry if multiple BJTs are present.")
        elif is_res(cell):
            add("info", inst, "resistor_device", "Resistor primitive detected.", "For precision analog resistors, consider common-centroid, dummies, same orientation, and matching environment.")
    if match_report and match_report.get("groups"):
        by_inst = {e["device"].get("inst"): e["device"] for e in entries}
        for g in match_report.get("groups", []):
            insts = g.get("instances", [])
            bodies = {mos_role_nets(by_inst[i]).get("B") for i in insts if i in by_inst and is_mos(by_inst[i].get("cell", ""))}
            sources = {mos_role_nets(by_inst[i]).get("S") for i in insts if i in by_inst and is_mos(by_inst[i].get("cell", ""))}
            if len(bodies - {""}) > 1:
                add("high", ",".join(insts), "matched_group_body_mismatch", f"Matched group {g.get('id')} has multiple body nets: {sorted(bodies)}.", "Use a consistent body/well strategy for matched devices or split groups intentionally.")
            if g.get("type", "").startswith("differential") and len(sources - {""}) > 1:
                add("high", ",".join(insts), "diffpair_source_mismatch", f"Differential-pair candidate {g.get('id')} has multiple source nets: {sorted(sources)}.", "Verify pair detection and tail/source node. Pair devices should normally share a source/tail node.")
    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    warnings.sort(key=lambda w: (severity_rank.get(w.get("severity"), 9), w.get("category", ""), w.get("inst", "")))
    return {"layout_warnings": True, "count": len(warnings), "warnings": warnings}


def add_topology_card(layout, top, entries, topology_report):
    if not ANALOGSENSE_CARD or not ADD_LABELS or not topology_report.get("topology_analysis"):
        return {"topology_card": False}
    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs.extend([x0, x1]); ys.extend([y0, y1])
    if not xs:
        return {"topology_card": False, "reason": "no extents"}
    layer = layout.layer(*TEXT_LAYER)
    x = max(xs) + MAP_OFFSET_UM
    y = min(ys) - float(os.environ.get("SKY_TOPOLOGY_CARD_DROP_UM", "25"))
    lines = ["ANALOGSENSE TOPOLOGY", "--------------------------------"]
    for g in topology_report.get("groups", [])[:ANALOG_CARD_TOP_N]:
        lines.append(f"{g.get('id')} {g.get('type')}: {','.join(g.get('instances', []))}")
    add_text(layout, top, layer, "\n".join(lines), x, y, DEVICE_MAP_MAG)
    return {"topology_card": True, "x_um": x, "y_um": y, "groups_shown": min(ANALOG_CARD_TOP_N, len(topology_report.get("groups", [])))}


def add_layout_warnings_card(layout, top, entries, warnings_report):
    if not LAYOUT_WARNING_CARD or not ADD_LABELS or not warnings_report.get("warnings"):
        return {"layout_warnings_card": False}
    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs.extend([x0, x1]); ys.extend([y0, y1])
    if not xs:
        return {"layout_warnings_card": False, "reason": "no extents"}
    layer = layout.layer(*TEXT_LAYER)
    x = max(xs) + MAP_OFFSET_UM
    y = min(ys) - float(os.environ.get("SKY_WARNING_CARD_DROP_UM", "65"))
    lines = ["LAYOUT REVIEW WARNINGS", "severity inst category", "--------------------------------"]
    for w in warnings_report.get("warnings", [])[:WARNING_CARD_TOP_N]:
        lines.append(f"{w.get('severity')} {w.get('inst')} {w.get('category')}")
    add_text(layout, top, layer, "\n".join(lines), x, y, DEVICE_MAP_MAG)
    return {"layout_warnings_card": True, "x_um": x, "y_um": y, "warnings_shown": min(WARNING_CARD_TOP_N, len(warnings_report.get("warnings", [])))}


def write_analogsense_outputs(out_gds, topology_report, warnings_report, net_stats=None):
    if not WRITE_ANALOGSENSE_FILES:
        return {"written": False}
    topology_path = out_gds + ".topology.json"
    warnings_csv = out_gds + ".layout_warnings.csv"
    warnings_json = out_gds + ".layout_warnings.json"
    priority_json = out_gds + ".routing_recommendations.json"
    with open(topology_path, "w") as f:
        json.dump(json_safe(topology_report), f, indent=2)
    with open(warnings_json, "w") as f:
        json.dump(json_safe(warnings_report), f, indent=2)
    rows = warnings_report.get("warnings", []) if warnings_report else []
    keys = ["severity", "inst", "category", "message", "recommendation"]
    with open(warnings_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in keys})
    with open(priority_json, "w") as f:
        json.dump(json_safe(net_stats or []), f, indent=2)
    return {"written": True, "topology_json": topology_path, "layout_warnings_csv": warnings_csv, "layout_warnings_json": warnings_json, "routing_recommendations_json": priority_json, "warnings": len(rows), "topology_groups": len(topology_report.get("groups", [])) if topology_report else 0}

def analog_device_width_units(device):
    """Effective width units used only for ratio/reporting, not geometry generation."""
    try:
        return float(device.get("w_um") or 0.0) * max(1, int(device.get("nf") or 1)) * max(1, int(device.get("m") or 1))
    except Exception:
        return 0.0


def analog_mirror_ratio(ref_device, device):
    ref_u = analog_device_width_units(ref_device)
    dev_u = analog_device_width_units(device)
    if ref_u <= 0:
        return None
    try:
        # If L is different, flag ratio but keep width-ratio as the practical mirror hint.
        return dev_u / ref_u
    except Exception:
        return None


def analog_is_diode_mos(device):
    if not is_mos(device.get("cell", "")):
        return False
    n = mos_role_nets(device)
    return bool(n.get("D") and n.get("D") == n.get("G"))


def analog_detect_current_mirror_groups(entries):
    """Detect analog current-mirror style groups including ratioed outputs.

    This intentionally goes beyond the earlier same-W/L mirror detector.  For
    analog layout, a diode-connected reference can feed ratioed outputs, so the
    physical placement should still align/gather those devices even when W differs.
    """
    mos_entries = [e for e in entries if is_mos(e.get("device", {}).get("cell", ""))]
    groups = []
    used_members = set()
    gid = 1

    refs = [e for e in mos_entries if analog_is_diode_mos(e["device"])]
    for ref in sorted(refs, key=lambda e: str(e["device"].get("inst", ""))):
        rd = ref["device"]
        rn = mos_role_nets(rd)
        rkind = mos_kind(rd.get("cell", ""))
        # Common analog mirror condition: same MOS polarity, same source/body, same gate bus.
        members = []
        for e in mos_entries:
            d = e["device"]
            n = mos_role_nets(d)
            if mos_kind(d.get("cell", "")) != rkind:
                continue
            if n.get("G") != rn.get("G"):
                continue
            if n.get("S") != rn.get("S"):
                continue
            if n.get("B") != rn.get("B"):
                continue
            members.append(e)
        if len(members) < 2:
            continue
        # Avoid duplicating the same exact group if another diode-connected member sees it.
        member_ids = tuple(sorted(e["device"].get("inst", "") for e in members))
        if member_ids in used_members:
            continue
        used_members.add(member_ids)

        existing_id = rd.get("topology_group") or f"AMIRR{gid}"
        gid += 1
        outputs = [e for e in members if e is not ref]
        ratios = {}
        for e in members:
            d = e["device"]
            r = analog_mirror_ratio(rd, d)
            ratios[d.get("inst", "")] = None if r is None else round(float(r), 4)
            d["topology_class"] = d.get("topology_class") or "current_mirror"
            d["topology_role"] = "mirror_reference" if e is ref else "mirror_output"
            d["topology_group"] = existing_id
            e["topology_group"] = existing_id
            e["analog_group_id"] = existing_id
            e["analog_group_type"] = "ratioed_current_mirror" if any((ratios.get(o["device"].get("inst")) not in (None, 1.0)) for o in outputs) else "current_mirror"
        groups.append({
            "id": existing_id,
            "type": "ratioed_current_mirror" if any((ratios.get(o["device"].get("inst")) not in (None, 1.0)) for o in outputs) else "current_mirror",
            "kind": rkind,
            "reference": rd.get("inst"),
            "gate_net": rn.get("G"),
            "source_net": rn.get("S"),
            "body_net": rn.get("B"),
            "instances": [e["device"].get("inst") for e in members],
            "outputs": [e["device"].get("inst") for e in outputs],
            "ratios_vs_reference": ratios,
            "reason": "diode-connected reference drives same-polarity devices with common source/body; dimensions may be ratioed",
            "entries": members,
        })
    return groups


def analog_entry_sort_key(entry):
    d = entry.get("device", {})
    n = mos_role_nets(d) if is_mos(d.get("cell", "")) else {}
    return (
        str(d.get("topology_group", "")),
        str(entry.get("analog_group_id", "")),
        str(d.get("match_group_id", "")),
        str(n.get("G", "")),
        str(d.get("cell", "")),
        str(d.get("inst", "")),
    )


def analog_chain_sort_key(entry):
    d = entry.get("device", {})
    n = mos_role_nets(d)
    # diode-connected / reference devices first when a chain branches.
    ref_score = 0 if analog_is_diode_mos(d) else 1
    return (ref_score, str(n.get("G", "")), str(d.get("inst", "")))


def analog_build_mos_stack_chains(items):
    """Build visual source-to-drain stack columns from MOS devices.

    This is a placement heuristic.  It keeps devices that share D/S internal nets
    close in the same column where possible, and leaves branches as separate
    columns so the generated layout stays readable and DRC-safe.
    """
    remaining = list(items)
    chains = []
    while remaining:
        drain_nets = {mos_role_nets(e["device"]).get("D") for e in remaining}
        starts = []
        for e in remaining:
            n = mos_role_nets(e["device"])
            s_net = n.get("S")
            if is_power_like_net(s_net) or s_net not in drain_nets:
                starts.append(e)
        start = sorted(starts or remaining, key=analog_chain_sort_key)[0]
        chain = [start]
        remaining.remove(start)
        cur = start
        seen_nets = set()
        while True:
            cur_d = mos_role_nets(cur["device"]).get("D")
            if not cur_d or cur_d in seen_nets:
                break
            seen_nets.add(cur_d)
            candidates = [e for e in remaining if mos_role_nets(e["device"]).get("S") == cur_d]
            if not candidates:
                break
            # Keep the most reference-like candidate in this column; branches become their own columns.
            nxt = sorted(candidates, key=analog_chain_sort_key)[0]
            chain.append(nxt)
            remaining.remove(nxt)
            cur = nxt
        chains.append(chain)
    # Longer stacks first, then deterministic names.
    chains.sort(key=lambda ch: (-len(ch), [e["device"].get("inst", "") for e in ch]))
    return chains


def analog_chain_nets(chain):
    nets = []
    for e in chain:
        for role, net in pin_roles_for_device(e["device"]):
            if net and net not in nets:
                nets.append(net)
    return nets


def compute_positions(entries):
    """AnalogPlacer placement with real bbox spacing.

    Compared with AnalogSense v2, this version makes the physical arrangement
    visibly analog-oriented:
      * current-mirror groups are placed as aligned rows with ratio reports
      * NMOS/PFET stack candidates are placed as source-to-drain columns
      * BJT/reference devices are placed near columns that share sensitive nets
      * unrelated devices are kept in clean fallback rows
    """
    positions = {}
    mos_entries = [e for e in entries if is_mos(e["device"]["cell"])]
    other_entries = [e for e in entries if not is_mos(e["device"]["cell"])]

    if PLACE_MODE == "ultra":
        x_margin = max(ULTRA_X_MARGIN_UM, 4.0)
        y_margin = max(ULTRA_Y_MARGIN_UM, 6.0)
    elif PLACE_MODE == "safe":
        x_margin = max(12.0, X_SPACING_UM * 0.45)
        y_margin = max(16.0, Y_SPACING_UM * 0.50)
    else:
        x_margin = max(COMPACT_X_MARGIN_UM, 8.0)
        y_margin = max(COMPACT_Y_MARGIN_UM, 10.0)

    analog_x_gap = max(x_margin, float(os.environ.get("SKY_ANALOG_COL_GAP_UM", str(ANALOG_COL_GAP_UM))))
    analog_y_gap = max(y_margin, float(os.environ.get("SKY_ANALOG_ROW_GAP_UM", str(ANALOG_ROW_GAP_UM))))
    stack_gap = max(float(os.environ.get("SKY_ANALOG_STACK_GAP_UM", str(ANALOG_STACK_GAP_UM))), 4.0)
    group_gap = max(float(os.environ.get("SKY_ANALOG_GROUP_GAP_UM", "18")), analog_x_gap)
    mirror_ref_first = os.environ.get("SKY_ANALOG_MIRROR_REF_FIRST", "1") == "1"

    if PLACEMENT_STYLE in ("schematic", "spice", "netlist", "schematic_order"):
        # Schematic placement intentionally avoids topology cleverness:
        # keep the original SPICE/schematic instance order, but optionally put
        # large MOS devices near each other in one top row for readability.
        positions = {}
        placement_rows = []
        y_cursor = 0.0
        max_x = 0.0
        min_y = 0.0

        def is_large_for_schematic(e):
            d = e.get("device", {})
            try:
                return is_mos(d.get("cell", "")) and float(d.get("w_um") or 0.0) >= SCHEMATIC_LARGE_W_UM
            except Exception:
                return False

        def place_single_row(items, y, row_name, start_x=0.0):
            x = start_x
            max_h = 0.0
            row = {"name": row_name, "instances": [], "large_row": row_name == "large_devices"}
            for e in items:
                w, h = bbox_um(e)
                positions[id(e)] = (x, y)
                row["instances"].append(e["device"].get("inst"))
                x += w + x_margin
                max_h = max(max_h, h)
            if row["instances"]:
                placement_rows.append(row)
            return x, max_h

        large_entries = [e for e in entries if is_large_for_schematic(e)] if SCHEMATIC_LARGE_ROW else []
        large_ids = {id(e) for e in large_entries}
        rest_entries = [e for e in entries if id(e) not in large_ids]

        if large_entries:
            x_end, row_h = place_single_row(large_entries, y_cursor, "large_devices", 0.0)
            max_x = max(max_x, x_end)
            y_cursor -= row_h + y_margin
            min_y = min(min_y, y_cursor)

        x = 0.0
        row_h = 0.0
        col = 0
        cur = []
        row_idx = 1
        for e in rest_entries:
            w, h = bbox_um(e)
            if col >= MAX_COLS:
                if cur:
                    placement_rows.append({"name": f"schematic_row_{row_idx}", "instances": cur, "large_row": False})
                    row_idx += 1
                cur = []
                x = 0.0
                y_cursor -= row_h + y_margin
                min_y = min(min_y, y_cursor)
                row_h = 0.0
                col = 0
            positions[id(e)] = (x, y_cursor)
            cur.append(e["device"].get("inst"))
            x += w + x_margin
            max_x = max(max_x, x)
            row_h = max(row_h, h)
            col += 1
        if cur:
            placement_rows.append({"name": f"schematic_row_{row_idx}", "instances": cur, "large_row": False})

        return positions, {
            "mos_total": len(mos_entries),
            "paired_mos_pairs": 0,
            "unmatched_mos": len(mos_entries),
            "other_entries": len(other_entries),
            "placement": "schematic_order_large_row" if large_entries else "schematic_order_rows",
            "placement_style": PLACEMENT_STYLE,
            "place_mode": PLACE_MODE,
            "layout_style": LAYOUT_STYLE,
            "x_margin_um": x_margin,
            "y_margin_um": y_margin,
            "schematic_large_row": SCHEMATIC_LARGE_ROW,
            "schematic_large_w_um": SCHEMATIC_LARGE_W_UM,
            "large_row_instances": [e["device"].get("inst") for e in large_entries],
            "rows": placement_rows,
            "estimated_width_um": max_x,
            "estimated_bottom_y_um": min_y,
        }

    def pack_row(items, y, start_x=0.0, gap=None):
        gap = analog_x_gap if gap is None else gap
        x = start_x
        max_h = 0.0
        placed_items = []
        for e in items:
            w, h = bbox_um(e)
            positions[id(e)] = (x, y)
            placed_items.append({"inst": e["device"].get("inst"), "x_um": x, "y_um": y, "w_um": w, "h_um": h})
            x += w + gap
            max_h = max(max_h, h)
        return {"x_end": x, "height": max_h, "placed": placed_items}

    def pack_wrapped_rows(items, start_y, start_x=0.0, max_cols=None):
        max_cols = max_cols or MAX_COLS
        x = start_x
        y = start_y
        col = 0
        row_h = 0.0
        max_x = start_x
        min_y = y
        rows = []
        cur_row = []
        for e in items:
            w, h = bbox_um(e)
            if col >= max_cols:
                rows.append(cur_row)
                cur_row = []
                x = start_x
                y -= row_h + analog_y_gap
                col = 0
                row_h = 0.0
            positions[id(e)] = (x, y)
            cur_row.append(e["device"].get("inst"))
            x += w + analog_x_gap
            max_x = max(max_x, x)
            row_h = max(row_h, h)
            min_y = min(min_y, y)
            col += 1
        if cur_row:
            rows.append(cur_row)
        return {"min_y": min_y, "max_x": max_x, "rows": rows, "height": (start_y - min_y + row_h) if items else 0.0}

    if LAYOUT_STYLE in ("analog", "mixed") and os.environ.get("SKY_ANALOG_PLACER", "1") == "1":
        mirror_groups = analog_detect_current_mirror_groups(entries)
        mirror_member_ids = set()
        for g in mirror_groups:
            for e in g.get("entries", []):
                mirror_member_ids.add(id(e))

        pfet_mirror_groups = [g for g in mirror_groups if g.get("kind") == "pfet"]
        nfet_mirror_groups = [g for g in mirror_groups if g.get("kind") == "nfet"]

        y_cursor = 0.0
        max_x = 0.0
        min_y = 0.0
        placement_report = {
            "engine": "analogplacer_v3",
            "mirror_groups": [],
            "nfet_stack_chains": [],
            "pfet_stack_chains": [],
            "reference_devices": [],
            "unassigned_rows": [],
        }

        # 1) PMOS mirrors at the top.  This mirrors real analog practice: shared
        # well/source/body devices are aligned, making the mirror gate bus obvious.
        x_cursor = 0.0
        row_h = 0.0
        for g in sorted(pfet_mirror_groups, key=lambda gg: str(gg.get("id", ""))):
            ref_inst = g.get("reference")
            ref_entries = [e for e in g["entries"] if e["device"].get("inst") == ref_inst]
            out_entries = [e for e in g["entries"] if e["device"].get("inst") != ref_inst]
            out_entries = sorted(out_entries, key=lambda e: (g.get("ratios_vs_reference", {}).get(e["device"].get("inst"), 0) or 0, str(e["device"].get("inst"))))
            ordered = (ref_entries + out_entries) if mirror_ref_first else sorted(g["entries"], key=analog_entry_sort_key)
            row = pack_row(ordered, y_cursor, x_cursor, gap=analog_x_gap)
            insts = [e["device"].get("inst") for e in ordered]
            placement_report["mirror_groups"].append({
                "id": g.get("id"), "kind": g.get("kind"), "reference": ref_inst,
                "instances_in_physical_order": insts,
                "gate_net": g.get("gate_net"), "source_net": g.get("source_net"), "body_net": g.get("body_net"),
                "ratios_vs_reference": g.get("ratios_vs_reference"),
                "placement": "aligned_horizontal_mirror_row",
            })
            x_cursor = row["x_end"] + group_gap
            row_h = max(row_h, row["height"])
            max_x = max(max_x, x_cursor)
        if pfet_mirror_groups:
            y_cursor -= row_h + analog_y_gap
            min_y = min(min_y, y_cursor)

        # 2) Remaining PMOS as rows or stacks.  Usually these are loads/current
        # sources, so keep them near the top but separate from detected mirrors.
        remaining_pfets = sorted([e for e in mos_entries if mos_kind(e["device"]["cell"]) == "pfet" and id(e) not in mirror_member_ids], key=analog_entry_sort_key)
        if remaining_pfets:
            pfet_chains = analog_build_mos_stack_chains(remaining_pfets)
            x_cursor = 0.0
            max_col_h = 0.0
            for chain in pfet_chains:
                # For PFET, source is usually top rail; place source-side first.
                y_chain = y_cursor
                col_w = 0.0
                instances = []
                for e in chain:
                    w, h = bbox_um(e)
                    positions[id(e)] = (x_cursor, y_chain - h)
                    y_chain -= h + stack_gap
                    col_w = max(col_w, w)
                    instances.append(e["device"].get("inst"))
                max_col_h = max(max_col_h, y_cursor - y_chain)
                placement_report["pfet_stack_chains"].append({"instances": instances, "nets": analog_chain_nets(chain), "placement": "vertical_pfet_source_to_drain_column"})
                x_cursor += col_w + analog_x_gap
                max_x = max(max_x, x_cursor)
            y_cursor -= max_col_h + analog_y_gap
            min_y = min(min_y, y_cursor)

        # 3) NMOS mirror groups not already part of stack rows.  This catches
        # diode/reference NMOS mirror structures if present.
        used_nfet_mirror_ids = set()
        nfet_group_entries = []
        for g in nfet_mirror_groups:
            for e in g.get("entries", []):
                nfet_group_entries.append(e)
                used_nfet_mirror_ids.add(id(e))
        if nfet_group_entries:
            row = pack_row(sorted(nfet_group_entries, key=analog_entry_sort_key), y_cursor, 0.0, gap=analog_x_gap)
            placement_report["mirror_groups"].append({
                "id": "NMOS_MIRROR_ROW", "kind": "nfet",
                "instances_in_physical_order": [e["device"].get("inst") for e in sorted(nfet_group_entries, key=analog_entry_sort_key)],
                "placement": "aligned_horizontal_nmos_mirror_row",
            })
            y_cursor -= row["height"] + analog_y_gap
            max_x = max(max_x, row["x_end"])
            min_y = min(min_y, y_cursor)

        # 4) Remaining NMOS as stack columns.  These visually follow source/drain
        # continuity so cascoded/reference branches are easier to inspect.
        remaining_nfets = sorted([e for e in mos_entries if mos_kind(e["device"]["cell"]) == "nfet" and id(e) not in used_nfet_mirror_ids], key=analog_entry_sort_key)
        n_chains = analog_build_mos_stack_chains(remaining_nfets)
        chain_x_centers = []
        x_cursor = 0.0
        max_stack_h = 0.0
        for chain in n_chains:
            heights = [bbox_um(e)[1] for e in chain]
            widths = [bbox_um(e)[0] for e in chain]
            total_h = sum(heights) + max(0, len(chain) - 1) * stack_gap
            y_chain = y_cursor - total_h
            cy = y_chain
            instances = []
            for e, h in zip(chain, heights):
                positions[id(e)] = (x_cursor, cy)
                cy += h + stack_gap
                instances.append(e["device"].get("inst"))
            col_w = max(widths or [1.0])
            chain_nets = analog_chain_nets(chain)
            chain_x_centers.append({"x": x_cursor + col_w / 2.0, "width": col_w, "nets": set(chain_nets), "instances": instances})
            placement_report["nfet_stack_chains"].append({"instances": instances, "nets": chain_nets, "placement": "vertical_nfet_source_to_drain_column"})
            x_cursor += col_w + analog_x_gap
            max_stack_h = max(max_stack_h, total_h)
            max_x = max(max_x, x_cursor)
        if n_chains:
            y_cursor -= max_stack_h + analog_y_gap
            min_y = min(min_y, y_cursor)

        # 5) BJT/reference/fixed primitives placed close to the most related NMOS
        # chain if they share sensitive nets.  This is useful for BGRs.
        bjts = sorted([e for e in other_entries if is_bjt(e["device"].get("cell", ""))], key=analog_entry_sort_key)
        other_non_bjts = sorted([e for e in other_entries if not is_bjt(e["device"].get("cell", ""))], key=analog_entry_sort_key)
        if bjts:
            bjt_positions = []
            for b in bjts:
                b_nets = set(b["device"].get("nodes", []))
                best = None
                best_score = -1
                for ch in chain_x_centers:
                    score = len((b_nets & ch["nets"]) - {"", None})
                    # Strong bonus for BJT emitter/base/collector sensitive nets such as net03.
                    if any(n in ch["nets"] for n in b_nets if str(n).lower().startswith("net")):
                        score += 1
                    if score > best_score:
                        best_score = score
                        best = ch
                w, h = bbox_um(b)
                bx = (best["x"] - w / 2.0) if best else 0.0
                by = y_cursor - h
                positions[id(b)] = (max(0.0, bx), by)
                bjt_positions.append({"inst": b["device"].get("inst"), "near_chain": best.get("instances") if best else [], "shared_nets": sorted(list((b_nets & (best["nets"] if best else set())) - {"", None})), "x_um": max(0.0, bx), "y_um": by})
                max_x = max(max_x, max(0.0, bx) + w)
                min_y = min(min_y, by)
            placement_report["reference_devices"] = bjt_positions
            y_cursor = min_y - analog_y_gap

        # 6) Remaining caps/resistors/IO etc. in clean rows at bottom.
        if other_non_bjts:
            o_report = pack_wrapped_rows(other_non_bjts, y_cursor, 0.0, max_cols=MAX_COLS)
            placement_report["unassigned_rows"] = o_report.get("rows", [])
            max_x = max(max_x, o_report.get("max_x", 0.0))
            min_y = min(min_y, o_report.get("min_y", y_cursor))

        return positions, {
            "mos_total": len(mos_entries),
            "paired_mos_pairs": 0,
            "unmatched_mos": len(mos_entries),
            "other_entries": len(other_entries),
            "placement": "analogplacer_v3_current_mirror_rows_stack_columns_reference_nearby",
            "place_mode": PLACE_MODE,
            "layout_style": LAYOUT_STYLE,
            "x_margin_um": analog_x_gap,
            "y_margin_um": analog_y_gap,
            "stack_gap_um": stack_gap,
            "group_gap_um": group_gap,
            "estimated_width_um": max_x,
            "estimated_bottom_y_um": min_y,
            "analog_placement": placement_report,
        }

    # Fallback/digital/simple mode: robust bbox row packing.
    sorted_entries = sorted(entries, key=analog_entry_sort_key)
    x = 0.0
    y = 0.0
    col = 0
    row_h = 0.0
    max_x = 0.0
    min_y = 0.0
    rows = []
    cur_row = []
    for entry in sorted_entries:
        w, h = bbox_um(entry)
        if col >= MAX_COLS:
            rows.append(cur_row)
            cur_row = []
            x = 0.0
            y -= row_h + y_margin
            col = 0
            row_h = 0.0
        positions[id(entry)] = (x, y)
        cur_row.append(entry["device"].get("inst"))
        x += w + x_margin
        max_x = max(max_x, x)
        row_h = max(row_h, h)
        min_y = min(min_y, y)
        col += 1
    if cur_row:
        rows.append(cur_row)
    return positions, {
        "mos_total": len(mos_entries),
        "paired_mos_pairs": 0,
        "unmatched_mos": len(mos_entries),
        "other_entries": len(other_entries),
        "placement": "simple_bbox_rows_fallback",
        "place_mode": PLACE_MODE,
        "layout_style": LAYOUT_STYLE,
        "x_margin_um": x_margin,
        "y_margin_um": y_margin,
        "estimated_width_um": max_x,
        "estimated_bottom_y_um": min_y,
        "rows": rows,
    }

def set_klayout_instance_properties(inst_obj, device, method):
    if not SET_INSTANCE_PROPERTIES or inst_obj is None:
        return False
    try:
        props = {
            "spice_inst": device.get("inst", ""),
            "spice_cell": device.get("cell", ""),
            "category": device_category(device.get("cell", "")),
            "method": method,
            "raw_spice": device.get("raw", ""),
            "nodes": ",".join(device.get("nodes", [])),
            "match_group_id": device.get("match_group_id", ""),
            "match_group_type": device.get("match_group_type", ""),
            "topology_class": device.get("topology_class", ""),
            "topology_role": device.get("topology_role", ""),
            "topology_group": device.get("topology_group", ""),
            "layout_style": LAYOUT_STYLE,
        }
        if is_mos(device.get("cell", "")):
            nets = mos_role_nets(device)
            props.update({f"net_{k}": v for k, v in nets.items()})
            props.update({"w_um": device.get("w_um"), "l_um": device.get("l_um"), "nf": device.get("nf"), "m": device.get("m")})
        else:
            for role, net in pin_roles_for_device(device):
                props[f"net_{role}"] = net
        for k, v in props.items():
            try:
                inst_obj.set_property(k, "" if v is None else str(v))
            except Exception:
                pass
        return True
    except Exception:
        return False


def write_device_map_outputs(out_gds, entries, net_stats=None, match_report=None):
    if not WRITE_DEVICE_MAP_FILES:
        return {"written": False}
    csv_path = out_gds + ".device_map.csv"
    json_path = out_gds + ".nets.json"
    priority_csv_path = out_gds + ".routing_priority.csv"
    match_json_path = out_gds + ".match_groups.json"
    rows = []
    nets = {}
    for e in entries:
        d = e["device"]
        role_map = {role: net for role, net in pin_roles_for_device(d)}
        row = {
            "inst": d.get("inst"),
            "cell": d.get("cell"),
            "category": device_category(d.get("cell")),
            "method": e.get("method"),
            "guard_ring": bool(d.get("guard_ring")),
            "match_group_id": d.get("match_group_id", ""),
            "match_group_type": d.get("match_group_type", ""),
            "w_um": d.get("w_um"),
            "l_um": d.get("l_um"),
            "nf": d.get("nf"),
            "m": d.get("m"),
            "nodes_spice_order": ",".join(d.get("nodes", [])),
            "raw_spice": d.get("raw"),
        }
        for role, net in role_map.items():
            row[f"net_{role}"] = net
            nets.setdefault(net, []).append({"inst": d.get("inst"), "role": role})
        rows.append(row)
    # union of keys to avoid dropping non-MOS roles
    keys = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    with open(json_path, "w") as f:
        json.dump(json_safe({"devices": rows, "nets": nets, "routing_priority": net_stats or []}), f, indent=2)

    priority_written = False
    if ROUTING_PRIORITY_REPORT and net_stats:
        pkeys = ["net", "pin_count", "inst_count", "hpwl_um", "x_span_um", "y_span_um", "is_power", "net_class", "criticality", "guide_layer", "priority_score", "recommendation", "instances", "roles"]
        with open(priority_csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=pkeys)
            writer.writeheader()
            for r in net_stats:
                rr = dict(r)
                rr["instances"] = ",".join(rr.get("instances", []))
                rr["roles"] = ",".join(rr.get("roles", []))
                rr["guide_layer"] = "%s,%s" % tuple(rr.get("guide_layer", GUIDE_LAYER))
                writer.writerow({k: rr.get(k, "") for k in pkeys})
        priority_written = True

    match_written = False
    if match_report is not None:
        with open(match_json_path, "w") as f:
            json.dump(json_safe(match_report), f, indent=2)
        match_written = True

    return {
        "written": True,
        "csv": csv_path,
        "json": json_path,
        "routing_priority_csv": priority_csv_path if priority_written else None,
        "match_groups_json": match_json_path if match_written else None,
        "devices": len(rows),
        "nets": len(nets),
        "routing_nets": len(net_stats or []),
        "match_groups": len((match_report or {}).get("groups", [])) if match_report else 0,
    }


def infer_power_nets(metadata, devices):
    globals_ = set(metadata.get("globals", []))
    for d in devices:
        for n in d.get("nodes", []):
            if n.lower() in ("vdd", "vccd1", "vdda", "vpwr"):
                globals_.add(n)
            if n.lower() in ("0", "gnd", "vssd1", "vssa", "vgnd", "vss"):
                globals_.add(n)

    vdd = None
    gnd = None
    for n in sorted(globals_):
        if n.lower() in ("vdd", "vccd1", "vdda", "vpwr"):
            vdd = n if vdd is None else vdd
        if n.lower() in ("0", "gnd", "vssd1", "vssa", "vgnd", "vss"):
            gnd = "GND" if n == "0" and gnd is None else (n if gnd is None else gnd)
    return vdd, gnd


def add_power_rails(layout, top, entries, metadata):
    if not entries or not ADD_RAILS:
        return {"rails_added": False}

    vdd, gnd = infer_power_nets(metadata, [e["device"] for e in entries])
    if not vdd and not gnd:
        return {"rails_added": False, "reason": "no VDD/GND-like nets found"}

    xs = []
    ys = []
    for e in entries:
        cell = e["cell"]
        x, y = e.get("position_um", (0, 0))
        bbox = cell.bbox()
        xs.extend([x, x + bbox.width() * layout.dbu])
        ys.extend([y, y + bbox.height() * layout.dbu])

    if not xs or not ys:
        return {"rails_added": False, "reason": "no bbox extents"}

    x0 = min(xs) - 10
    x1 = max(xs) + 10
    y_top = max(ys) + 18
    y_bot = min(ys) - 18
    rail_h = 0.6

    m1_layer = layout.layer(*M1_RAIL_LAYER)
    m1_text_layer = layout.layer(*M1_TEXT_LAYER)

    if vdd:
        top.shapes(m1_layer).insert(pya.Box(dbu_um(layout, x0), dbu_um(layout, y_top), dbu_um(layout, x1), dbu_um(layout, y_top + rail_h)))
        add_text(layout, top, m1_text_layer, f"VDD RAIL: {vdd}", x0, y_top + 1.2, 1.0)
    if gnd:
        top.shapes(m1_layer).insert(pya.Box(dbu_um(layout, x0), dbu_um(layout, y_bot), dbu_um(layout, x1), dbu_um(layout, y_bot + rail_h)))
        add_text(layout, top, m1_text_layer, f"GND RAIL: {gnd}", x0, y_bot - 2.2, 1.0)

    return {"rails_added": True, "vdd_net": vdd, "gnd_net": gnd, "x0_um": x0, "x1_um": x1}



# -----------------------------------------------------------------------------
# Optional separate ratline reference GDS outputs
# -----------------------------------------------------------------------------

def _ratline_mode():
    return os.environ.get("SKY_RATLINE_GDS", os.environ.get("SKY_RATLINES", RATLINE_GDS_MODE)).strip().lower()

def _ratline_layer_tuple(env_name, default_value):
    raw = os.environ.get(env_name, default_value)
    try:
        a, b = raw.split(",", 1)
        return int(a), int(b)
    except Exception:
        return tuple(map(int, default_value.split(",")))

def _ratline_entry_anchors(entry):
    """Return absolute terminal anchors for ratline reference GDS."""
    try:
        if entry.get("pin_anchors"):
            return [dict(a) for a in entry.get("pin_anchors", [])]
    except Exception:
        pass
    try:
        d = entry["device"]
        x, y = entry.get("position_um", (0.0, 0.0))
        w, h = bbox_um(entry)
        return terminal_anchor_points(d, x, y, w, h)
    except Exception:
        pass
    out = []
    try:
        d = entry["device"]
        x, y = entry.get("position_um", (0.0, 0.0))
        for i, net in enumerate(list(d.get("nodes", []) or [])):
            out.append({"inst": d.get("inst", ""), "role": "N%d" % (i + 1), "net": net, "x_um": float(x), "y_um": float(y)})
    except Exception:
        pass
    return out

def create_ratline_reference_gds(layout, out_gds, entries):
    """Create optional separate ratline reference GDS files.

    The normal OUT_GDS is written before this function is called and remains clean.
    These extra GDS files use non-fabrication layers only:
      *_ratpoints.gds      terminal markers only
      *_ratlines_full.gds  connected non-fab reference lines
    """
    mode = _ratline_mode()
    if mode in ("0", "false", "no", "none", "off", "disable", "disabled", ""):
        return {"enabled": False, "mode": mode or "off"}
    if mode == "ask":
        if not can_prompt():
            return {"enabled": False, "mode": "ask", "reason": "interactive input unavailable"}
        ans = input_or_default("Create separate ratline reference GDS? (off/points/full/both) [default both]: ", "both").strip().lower()
        mode = ans if ans in ("points", "full", "both", "off") else "both"
        os.environ["SKY_RATLINE_GDS"] = mode
        if mode == "off":
            return {"enabled": False, "mode": "off"}
    if mode not in ("points", "point", "markers", "full", "lines", "both", "all"):
        mode = "both"

    include_power = os.environ.get("SKY_RATLINE_INCLUDE_POWER", "0").strip().lower() in ("1", "true", "yes", "on")
    point_layer = layout.layer(*_ratline_layer_tuple("SKY_RATPOINT_LAYER", "255,41"))
    line_layer = layout.layer(*_ratline_layer_tuple("SKY_RATLINE_LAYER", "255,42"))
    text_layer = layout.layer(*_ratline_layer_tuple("SKY_RATLINE_TEXT_LAYER", "255,43"))
    width = max(1, dbu_um(layout, float(os.environ.get("SKY_RATLINE_WIDTH_UM", "0.06"))))
    box_half = max(1, dbu_um(layout, float(os.environ.get("SKY_RATPOINT_HALF_UM", "0.18"))))
    top = layout.cell(TOP_NAME)
    if top is None:
        return {"enabled": False, "mode": mode, "reason": "top cell not found"}

    net_to_anchors = {}
    for e in entries:
        for a in _ratline_entry_anchors(e):
            net = a.get("net", "")
            if not net:
                continue
            if is_power_like_net(net) and not include_power:
                continue
            net_to_anchors.setdefault(net, []).append(a)

    point_count = 0
    for net, anchors in sorted(net_to_anchors.items()):
        for a in anchors:
            x = dbu_um(layout, float(a.get("x_um", 0.0)))
            y = dbu_um(layout, float(a.get("y_um", 0.0)))
            top.shapes(point_layer).insert(pya.Box(x - box_half, y - box_half, x + box_half, y + box_half))
            point_count += 1

    base, ext = os.path.splitext(out_gds)
    written = {}
    if mode in ("points", "point", "markers", "both", "all"):
        points_path = base + "_ratpoints.gds"
        layout.write(points_path)
        written["ratpoints_gds"] = points_path

    line_count = 0
    labelled = 0
    if mode in ("full", "lines", "both", "all"):
        for net, anchors in sorted(net_to_anchors.items()):
            if len(anchors) < 2:
                continue
            cx = sum(float(a["x_um"]) for a in anchors) / len(anchors)
            cy = sum(float(a["y_um"]) for a in anchors) / len(anchors)
            root = min(anchors, key=lambda a: (float(a["x_um"]) - cx) ** 2 + (float(a["y_um"]) - cy) ** 2)
            rp = pya.Point(dbu_um(layout, root["x_um"]), dbu_um(layout, root["y_um"]))
            for a in anchors:
                if a is root:
                    continue
                ap = pya.Point(dbu_um(layout, a["x_um"]), dbu_um(layout, a["y_um"]))
                top.shapes(line_layer).insert(pya.Path([rp, ap], width))
                line_count += 1
            add_text(layout, top, text_layer, "rat:" + str(net), cx, cy, 0.55)
            labelled += 1
        full_path = base + "_ratlines_full.gds"
        layout.write(full_path)
        written["ratlines_full_gds"] = full_path

    return {"enabled": True, "mode": mode, "include_power": include_power, "nets": len(net_to_anchors), "points": point_count, "segments": line_count, "labelled_nets": labelled, **written}

def run_magic_drc_check_on_output(gds_path, top_name):
    """Optional Magic DRC smoke check on the final GDS.

    This does not drive placement by itself; it records the Magic DRC transcript
    so the user can immediately see if compact/ultra spacing introduced errors.
    Enable with SKY_MAGIC_DRC_CHECK=1.
    """
    if not MAGIC_DRC_CHECK:
        return {"magic_drc_check": False}
    if not shutil.which(SKY_MAGIC_BIN):
        return {"magic_drc_check": False, "reason": f"Magic executable not found: {SKY_MAGIC_BIN}"}
    if not os.path.isfile(SKY_MAGIC_RC):
        return {"magic_drc_check": False, "reason": f"Magic rcfile not found: {SKY_MAGIC_RC}"}

    workdir = tempfile.mkdtemp(prefix="sky130_final_drc_")
    tcl_path = os.path.join(workdir, "run_final_drc.tcl")
    log_path = gds_path + ".magic_drc.log"
    tcl = f"""
drc style drc(full)
gds read {tcl_brace(gds_path)}
load {tcl_brace(top_name)} -quiet
select top cell
expand
puts "MAGIC_FINAL_DRC_BEGIN"
drc check
drc catchup
puts "MAGIC_FINAL_DRC_COUNT_BEGIN"
drc count
puts "MAGIC_FINAL_DRC_COUNT_END"
quit -noprompt
"""
    with open(tcl_path, "w") as f:
        f.write(tcl)
    env = os.environ.copy()
    env["MAGTYPE"] = "mag"
    env.setdefault("PDK_ROOT", PDK_ROOT)
    env.setdefault("PDK", PDK)
    env.setdefault("PDKPATH", PDKPATH)
    try:
        proc = subprocess.run(
            [SKY_MAGIC_BIN, "-dnull", "-noconsole", "-rcfile", SKY_MAGIC_RC, tcl_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            universal_newlines=True,
            timeout=float(os.environ.get("SKY_MAGIC_DRC_TIMEOUT_S", "180")),
        )
        with open(log_path, "w") as f:
            f.write("STDOUT\n======\n")
            f.write(proc.stdout or "")
            f.write("\n\nSTDERR\n======\n")
            f.write(proc.stderr or "")
        shutil.rmtree(workdir, ignore_errors=True)
        return {
            "magic_drc_check": True,
            "returncode": proc.returncode,
            "log": log_path,
            "stdout_tail": (proc.stdout or "").splitlines()[-30:],
            "stderr_tail": (proc.stderr or "").splitlines()[-30:],
        }
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return {"magic_drc_check": False, "reason": str(e)}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def backup_existing_output(path):
    if os.path.exists(path) and not OVERWRITE:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup = f"{path}.backup_{ts}"
        shutil.move(path, backup)
        print("Existing output moved to:", backup)


def get_args():
    global NETLIST, OUT_GDS
    args = [a for a in sys.argv[1:] if a != "--"]
    # KLayout sometimes passes script path in argv; accept first existing .spice/.sp/.cdl.
    if NETLIST is None:
        for a in args:
            if os.path.isfile(a) and os.path.splitext(a)[1].lower() in (".spice", ".sp", ".cdl", ".net", ".cir"):
                NETLIST = os.path.abspath(a)
                break
    if OUT_GDS is None:
        for a in args:
            if a.lower().endswith(".gds"):
                OUT_GDS = os.path.abspath(a)
                break


def main():
    global OUT_GDS
    get_args()

    if not NETLIST:
        die("No netlist provided. Use SKY_NETLIST=/path/file.spice or pass file after --")
    if not os.path.isfile(NETLIST):
        die("Netlist not found: %s" % NETLIST)
    if not os.path.isdir(PDKPATH):
        die("PDKPATH not found: %s" % PDKPATH)
    if CELLS_PY_ROOT is None:
        print("WARNING: Could not find libs.tech/klayout/python/cells or pymacros/cells. Static GDS can still work, direct draw may fail.")

    if not OUT_GDS:
        base = os.path.splitext(os.path.basename(NETLIST))[0]
        OUT_GDS = os.path.abspath(base + "_autoplaced_all.gds")
    else:
        OUT_GDS = os.path.abspath(OUT_GDS)

    OUT_GDS = setup_output_bundle_path(NETLIST, OUT_GDS)
    report_path = OUT_GDS + ".report.json"

    print("========== skyspice2klayout ALL-DEVICES ==========")
    print("PDK_ROOT          :", PDK_ROOT)
    print("PDK               :", PDK)
    print("PDKPATH           :", PDKPATH)
    print("KLAYOUT_PY_SITE   :", os.environ.get("KLAYOUT_PY_SITE", ""))
    print("SKY_GR_BACKEND    :", SKY_GR_BACKEND)
    print("SKY_PLACE_MODE    :", PLACE_MODE)
    print("SKY_PIN_LABELS    :", PIN_LABELS)
    print("SKY_NET_GUIDES    :", NET_GUIDES)
    print("SKY_GUIDE_MODE    :", GUIDE_MODE)
    print("SKY_GUIDE_STYLE   :", GUIDE_STYLE)
    print("SKY_TOP_GUIDES    :", TOP_GUIDES_ENABLED)
    print("SKY_MOVABLE_WRAP  :", MOVABLE_DEVICE_WRAPPERS)
    print("SKY_LOCAL_STUBS   :", LOCAL_PIN_STUBS)
    print("SKY_ANALOGSENSE   :", ANALOGSENSE)
    print("SKY_LAYOUT_STYLE  :", LAYOUT_STYLE)
    print("SKY_PLACEMENT_STYLE:", PLACEMENT_STYLE)
    print("SKY_RATLINE_GDS   :", _ratline_mode())
    print("SKY_MAGIC_ALL_GR  :", MAGIC_ALL_GUARD_RINGS)
    print("SKY_UNIT_ARRAY    :", UNIT_ARRAY_MODE, "minW=", UNIT_ARRAY_MIN_W_UM, "unitW=", UNIT_W_UM)
    print("SKY_GUIDE_NETS    :", GUIDE_NETS_RAW or "<auto>")
    print("SKY_MAP_SIDE      :", MAP_SIDE)
    print("SKY_MATCH_GROUPS  :", MATCH_GROUPS)
    print("SKY_MAGIC_BIN     :", SKY_MAGIC_BIN)
    print("SKY_MAGIC_RC      :", SKY_MAGIC_RC)
    print("CELLS_PY_ROOT     :", CELLS_PY_ROOT)
    print("FIXED_DEVICES_DIR :", FIXED_DEVICES_DIR)
    print("LIBS_REF_DIR      :", LIBS_REF_DIR)
    print("NETLIST           :", NETLIST)
    print("OUT_GDS           :", OUT_GDS)
    print("TOP               :", TOP_NAME)
    print("==================================================")

    # Scan actual PDK tree on this machine, recursively.
    fixed_index, fixed_lower_index = build_gds_index(FIXED_DEVICES_DIR)
    print("Fixed-device GDS cells indexed:", len(fixed_index))

    devices, metadata = parse_netlist(NETLIST)
    if not devices:
        die("No sky130_*__* devices/cells found in netlist.")

    print("\nParsed devices/cells:")
    for d in devices:
        print("  %-18s %-55s cat=%-13s W=%s L=%s nf=%s m=%s nodes=%s" % (
            d["inst"], d["cell"], d["category"], d.get("w_um"), d.get("l_um"),
            d.get("nf"), d.get("m"), ",".join(d.get("nodes", []))
        ))

    wizard_report = maybe_run_terminal_wizard(devices, metadata)

    # Optional selected guard rings. This uses draw_fet.py's native bulk='guard ring' path,
    # not a cosmetic rectangle overlay.
    guard_ring_insts, guard_ring_report = choose_guard_ring_instances(devices)
    if guard_ring_insts:
        for d in devices:
            if d.get("inst", "").lower() in guard_ring_insts:
                d["guard_ring"] = True
        print("\nGuard rings enabled for:", ", ".join(sorted(guard_ring_insts)))
    else:
        print("\nGuard rings: disabled")

    resolution_map = parse_device_map_env()
    if resolution_map:
        print("Device replacement map from SKY_DEVICE_MAP:", resolution_map)

    layout = pya.Layout()
    layout.dbu = LAYOUT_DBU
    top = layout.create_cell(TOP_NAME)

    loaded_gds = set()
    entries = []
    missing = []
    used = []

    for original_d in devices:
        d = original_d

        # Apply explicit mapping first, then try normal generation/import.
        if d.get("cell") in resolution_map:
            d = clone_device_with_cell(d, resolution_map[d.get("cell")], "env_or_prior_user_map")

        cell, method, detail = create_layout_cell_for_device(
            layout, d, loaded_gds, fixed_index, fixed_lower_index
        )

        # If missing, offer close alternatives and retry once.
        if cell is None and should_offer_fuzzy_alternate(d, fixed_index, fixed_lower_index):
            alternate = prompt_for_alternate(d, fixed_index, resolution_map)
            if alternate:
                d_alt = clone_device_with_cell(d, alternate, "interactive_fuzzy_match")
                if d.get("guard_ring"):
                    d_alt["guard_ring"] = True
                cell, method, detail = create_layout_cell_for_device(
                    layout, d_alt, loaded_gds, fixed_index, fixed_lower_index
                )
                if cell is not None:
                    d = d_alt

        if cell is None:
            missing.append({
                "inst": d["inst"],
                "cell": d["cell"],
                "original_cell": d.get("original_cell", d["cell"]),
                "category": d["category"],
                "reason": detail,
                "suggestions": json_safe(top_fuzzy_candidates(d, fixed_index, threshold=max(0.60, FUZZY_THRESHOLD - 0.15), top_n=FUZZY_TOP_N)) if ENABLE_FUZZY_MATCH else [],
                "raw": d["raw"],
            })
            continue

        entry = {"device": d, "cell": cell, "method": method, "detail": detail}
        entries.append(entry)
        used.append({
            "inst": d["inst"],
            "cell": d["cell"],
            "category": d["category"],
            "method": method,
            "original_cell": d.get("original_cell"),
            "alternate_reason": d.get("alternate_reason"),
            "guard_ring": bool(d.get("guard_ring")),
            "detail": json_safe(detail),
        })

    # Major analog synthesis pass: optionally replace large MOS devices by
    # equivalent parallel unit-array wrappers before placement/topology reports.
    unit_array_report = apply_unit_array_synthesis(layout, entries, loaded_gds, fixed_index, fixed_lower_index)

    match_report = detect_and_tag_matched_groups(entries)
    topology_report = analyze_circuit_topology(entries, metadata)
    warnings_report = build_layout_warnings(entries, topology_report, match_report, metadata)

    # Build movable annotated wrapper cells before placement.  The wrapper cell
    # contains the actual device plus its pin/net labels and local route stubs,
    # so moving the instance in KLayout moves those annotations with it.
    if MOVABLE_DEVICE_WRAPPERS:
        for entry in entries:
            make_annotated_device_wrapper(layout, entry)

    positions, placement_summary = compute_positions(entries)

    text_layer = layout.layer(*TEXT_LAYER) if ADD_LABELS else None
    pin_text_layer = layout.layer(*PIN_TEXT_LAYER) if ADD_LABELS else None

    placed = []
    for entry in entries:
        d = entry["device"]
        cell = entry["cell"]
        method = entry["method"]
        x_um, y_um = positions.get(id(entry), (0.0, 0.0))
        entry["position_um"] = (x_um, y_um)

        bbox = cell.bbox()
        tx = dbu_um(layout, x_um) - bbox.left
        ty = dbu_um(layout, y_um) - bbox.bottom
        inst_obj = top.insert(pya.CellInstArray(cell.cell_index(), pya.Trans(tx, ty)))
        set_klayout_instance_properties(inst_obj, d, method)

        if MOVABLE_DEVICE_WRAPPERS and entry.get("annotation_wrapper"):
            # Labels/stubs are inside the wrapper cell, so they move with the device.
            absolute_pin_anchors_for_entry(entry)
        elif ADD_LABELS:
            # Legacy top-level annotations.  These are static top-level shapes.
            add_text(layout, top, text_layer, d.get("inst", ""), x_um, y_um - 1.4, DEVICE_LABEL_MAG)
            add_pin_labels_for_entry(layout, top, entry, pin_text_layer)

        placed.append({
            "inst": d["inst"],
            "cell": d["cell"],
            "category": d["category"],
            "method": method,
            "original_cell": d.get("original_cell"),
            "alternate_reason": d.get("alternate_reason"),
            "guard_ring": bool(d.get("guard_ring")),
            "x_um": x_um,
            "y_um": y_um,
            "nodes": d.get("nodes", []),
            "mos_role_nets": mos_role_nets(d) if is_mos(d["cell"]) else None,
            "w_um": d.get("w_um"),
            "l_um": d.get("l_um"),
            "nf": d.get("nf"),
            "m": d.get("m"),
            "raw": d.get("raw"),
            "topology_class": d.get("topology_class"),
            "topology_role": d.get("topology_role"),
            "topology_group": d.get("topology_group"),
            "match_group_id": d.get("match_group_id"),
            "match_group_type": d.get("match_group_type"),
        })

    net_stats = build_net_statistics(entries)
    net_stats = enhance_routing_priority(entries, net_stats, topology_report, match_report)
    rails_report = add_power_rails(layout, top, entries, metadata)
    guides_report = add_net_guides(layout, top, entries, net_stats)
    map_card_report = add_device_map_card(layout, top, entries)
    routing_card_report = add_routing_priority_card(layout, top, entries, net_stats, map_card_report)
    match_card_report = add_match_group_card(layout, top, entries, match_report, map_card_report)
    topology_card_report = add_topology_card(layout, top, entries, topology_report)
    warnings_card_report = add_layout_warnings_card(layout, top, entries, warnings_report)
    device_map_files_report = write_device_map_outputs(OUT_GDS, entries, net_stats, match_report)
    analogsense_files_report = write_analogsense_outputs(OUT_GDS, topology_report, warnings_report, net_stats)
    unit_array_files_report = write_unit_array_outputs(OUT_GDS, unit_array_report)
    unit_array_card_report = add_unit_array_card(layout, top, entries, unit_array_report)

    if ADD_LABELS and SUMMARY_CARD:
        summary = [
            "skyspice2klayout ALL-DEVICES",
            "netlist: " + os.path.basename(NETLIST),
            "placed: %d missing: %d" % (len(placed), len(missing)),
        ]
        for subckt in metadata.get("subckts", [])[:4]:
            summary.append(subckt)
        if metadata.get("globals"):
            summary.append("globals: " + ",".join(metadata.get("globals", [])))
        for vs in metadata.get("voltage_sources", [])[:6]:
            summary.append("{}: {}-{} {}".format(vs["name"], vs["positive"], vs["negative"], vs["value"]))
        # Keep summary out of the device area; detailed maps/reports are written as files by default.
        sx = max([entry_bbox_extents_um(e)[2] for e in entries] or [0]) + MAP_OFFSET_UM
        sy = max([entry_bbox_extents_um(e)[3] for e in entries] or [0]) + float(os.environ.get("SKY_SUMMARY_CARD_RISE_UM", "25"))
        add_text(layout, top, text_layer, "\n".join(summary), sx, sy, 0.8)

    backup_existing_output(OUT_GDS)
    layout.write(OUT_GDS)
    ratline_report = create_ratline_reference_gds(layout, OUT_GDS, entries)
    drc_report = run_magic_drc_check_on_output(OUT_GDS, TOP_NAME)

    report = {
        "version": "all_devices_hybrid_v11_magicgr_analogproplus",
        "pdk_root": PDK_ROOT,
        "pdk": PDK,
        "pdkpath": PDKPATH,
        "klayout_py_site": os.environ.get("KLAYOUT_PY_SITE"),
        "cells_py_root": CELLS_PY_ROOT,
        "fixed_devices_dir": FIXED_DEVICES_DIR,
        "libs_ref_dir": LIBS_REF_DIR,
        "netlist": NETLIST,
        "out_gds": OUT_GDS,
        "top": TOP_NAME,
        "metadata": metadata,
        "wizard": wizard_report,
        "guard_rings": guard_ring_report,
        "device_resolution_map": resolution_map,
        "devices_total": len(devices),
        "placed_total": len(placed),
        "missing_total": len(missing),
        "placement_summary": placement_summary,
        "rails": rails_report,
        "net_guides": guides_report,
        "guide_style": GUIDE_STYLE,
        "top_guides_enabled": TOP_GUIDES_ENABLED,
        "movable_device_wrappers": MOVABLE_DEVICE_WRAPPERS,
        "local_pin_stubs": LOCAL_PIN_STUBS,
        "routing_priority": net_stats,
        "matched_groups": match_report,
        "topology": topology_report,
        "layout_warnings": warnings_report,
        "analogsense": {"enabled": ANALOGSENSE, "layout_style": LAYOUT_STYLE},
        "unit_arrays": unit_array_report,
        "device_map_card": map_card_report,
        "routing_priority_card": routing_card_report,
        "match_group_card": match_card_report,
        "topology_card": topology_card_report,
        "layout_warnings_card": warnings_card_report,
        "device_map_files": device_map_files_report,
        "analogsense_files": analogsense_files_report,
        "unit_array_files": unit_array_files_report,
        "unit_array_card": unit_array_card_report,
        "ratlines": ratline_report,
        "magic_drc": drc_report,
        "used": used,
        "placed": placed,
        "missing": missing,
    }

    # Write once so dashboard/manifest can see the JSON report, then rewrite with bundle metadata.
    with open(report_path, "w") as f:
        json.dump(json_safe(report), f, indent=2)
    output_bundle_report = write_output_bundle_files(report, report_path)
    report["output_bundle"] = output_bundle_report
    with open(report_path, "w") as f:
        json.dump(json_safe(report), f, indent=2)

    print("\n========== RESULT ALL-DEVICES ==========")
    print("Placed          :", len(placed))
    print("Missing         :", len(missing))
    print("Fixed GDS used  :", sum(1 for u in used if u["method"] == "static_fixed_gds"))
    print("libs.ref used   :", sum(1 for u in used if "libsref" in u["method"]))
    print("Direct draw used:", sum(1 for u in used if u["method"].startswith("direct_draw")))
    print("Guard-ring FETs :", sum(1 for u in used if u.get("guard_ring")))
    print("MOS pairs       :", placement_summary.get("paired_mos_pairs"))
    print("Placement style :", PLACEMENT_STYLE)
    print("Rails           :", rails_report)
    print("Net guides      :", guides_report)
    print("Ratline GDS     :", ratline_report)
    print("Routing priority:", {"nets": len(net_stats), "top": [r.get("net") for r in net_stats[:5]]})
    print("Matched groups  :", {"count": len(match_report.get("groups", [])), "enabled": match_report.get("match_groups_enabled")})
    print("Topology groups :", {"count": len(topology_report.get("groups", [])), "enabled": topology_report.get("topology_analysis")})
    print("Layout warnings :", {"count": warnings_report.get("count", 0), "enabled": warnings_report.get("layout_warnings")})
    print("Device map      :", device_map_files_report)
    print("AnalogSense     :", analogsense_files_report)
    print("Unit arrays     :", {"mode": unit_array_report.get("mode"), "planned": unit_array_report.get("devices_planned"), "replaced": unit_array_report.get("devices_layout_replaced")})
    print("Unit array files:", unit_array_files_report)
    print("Magic DRC       :", drc_report)
    print("Output GDS      :", OUT_GDS)
    print("Output folder   :", os.path.dirname(OUT_GDS))
    print("Dashboard       :", output_bundle_report.get("dashboard") if isinstance(output_bundle_report, dict) else None)
    print("Report          :", report_path)

    if missing:
        print("\nMissing/unplaced:")
        for m in missing:
            print("  %-18s %-55s %s" % (m["inst"], m["cell"], m["reason"]))

    print("\nOpen with:")
    print('  KLAYOUT_PATH="%s" klayout -e "%s"' % (os.path.join(PDKPATH, "libs.tech", "klayout"), OUT_GDS))
    print("========================================")

    if OPEN_AFTER:
        os.system('KLAYOUT_PATH="%s" klayout -e "%s" &' % (os.path.join(PDKPATH, "libs.tech", "klayout"), OUT_GDS))



# -----------------------------------------------------------------------------
# AnalogPro v10 additions: constraints, common-centroid planning, template markers,
# dummy-device planning, routing plan, and Magic/Netgen LVS helper.
# Inserted as runtime overrides before main() so the proven v9 flow stays intact.
# -----------------------------------------------------------------------------

# User/flow controls
SKY_CONSTRAINTS = os.environ.get("SKY_CONSTRAINTS", "").strip()
ANALOGPRO_ENABLE = os.environ.get("SKY_ANALOGPRO", "1") == "1"
CC_ENABLE = os.environ.get("SKY_CC_ENABLE", "1") == "1"
CC_MODE = os.environ.get("SKY_CC_MODE", "plan").strip().lower()     # off|plan|markers|layout_hint
CC_STYLE = os.environ.get("SKY_CC_STYLE", "common_centroid").strip().lower()  # common_centroid|interdigitated|abba
CC_AUTO_MIRRORS = os.environ.get("SKY_CC_AUTO_MIRRORS", "1") == "1"
CC_MARKERS = os.environ.get("SKY_CC_MARKERS", "1") == "1"
CC_AXIS_LAYER = tuple(map(int, os.environ.get("SKY_CC_AXIS_LAYER", "255,40").split(",")))
CC_BOX_LAYER = tuple(map(int, os.environ.get("SKY_CC_BOX_LAYER", "255,41").split(",")))
CC_DUMMY_LAYER = tuple(map(int, os.environ.get("SKY_CC_DUMMY_LAYER", "255,42").split(",")))
CC_TEXT_LAYER = tuple(map(int, os.environ.get("SKY_CC_TEXT_LAYER", "255,43").split(",")))
DUMMY_MODE = os.environ.get("SKY_DUMMY_MODE", "marker").strip().lower()  # off|marker|physical_lvs_risky
DUMMY_COUNT = int(os.environ.get("SKY_DUMMY_COUNT", "1"))
TEMPLATE_MODE = os.environ.get("SKY_ANALOG_TEMPLATE", "auto").strip().lower()  # auto|bgr|mirror|stack|none
ROUTING_PLAN_ENABLE = os.environ.get("SKY_ROUTING_PLAN", "1") == "1"
LVS_CHECK = os.environ.get("SKY_LVS_CHECK", "0") == "1"
NETGEN_BIN = os.environ.get("SKY_NETGEN_BIN", "netgen").strip()
NETGEN_SETUP = os.environ.get("SKY_NETGEN_SETUP", os.path.join(PDKPATH, "libs.tech", "netgen", f"{PDK}_setup.tcl"))
EXT2SPICE_BLACKBOX = os.environ.get("SKY_EXT2SPICE_BLACKBOX", "0") == "1"

_ANALOGPRO_CACHE = {"constraints": None, "entries": None, "placement": None, "cc_plan": None, "routing_plan": None}


def _ap_warn(msg):
    print("[AnalogPro]", msg)


def _parse_scalar_simple(v):
    v = str(v).strip()
    if not v:
        return ""
    if v.lower() in ("true", "yes", "on"): return True
    if v.lower() in ("false", "no", "off"): return False
    if v.lower() in ("null", "none"): return None
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        body = v[1:-1].strip()
        if not body: return []
        return [_parse_scalar_simple(x.strip()) for x in body.split(",")]
    try:
        if "." in v:
            return float(v)
        return int(v)
    except Exception:
        return v


def _parse_simple_yaml(text):
    """Very small YAML subset parser for our constraint files.

    Supports top-level sections containing lists of dictionaries:
      mirror_groups:
        - name: M1
          reference: X8
          outputs: [X10, X11]
    This avoids requiring PyYAML inside KLayout.
    """
    data = {}
    current_key = None
    current_item = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            current_key = stripped[:-1].strip()
            data[current_key] = []
            current_item = None
            continue
        if current_key is None:
            continue
        if stripped.startswith("- "):
            rest = stripped[2:].strip()
            current_item = {}
            data.setdefault(current_key, []).append(current_item)
            if rest and ":" in rest:
                k, v = rest.split(":", 1)
                current_item[k.strip()] = _parse_scalar_simple(v.strip())
        elif current_item is not None and ":" in stripped:
            k, v = stripped.split(":", 1)
            current_item[k.strip()] = _parse_scalar_simple(v.strip())
    return data


def load_analogpro_constraints():
    if _ANALOGPRO_CACHE.get("constraints") is not None:
        return _ANALOGPRO_CACHE["constraints"]
    path = SKY_CONSTRAINTS
    if not path:
        obj = {"enabled": False, "source": "", "constraints": {}}
        _ANALOGPRO_CACHE["constraints"] = obj
        return obj
    if not os.path.isfile(path):
        _ap_warn(f"SKY_CONSTRAINTS path not found: {path}; continuing with auto constraints")
        obj = {"enabled": False, "source": path, "error": "file_not_found", "constraints": {}}
        _ANALOGPRO_CACHE["constraints"] = obj
        return obj
    try:
        txt = open(path, "r", errors="ignore").read()
        if path.lower().endswith(".json"):
            constraints = json.loads(txt)
        else:
            constraints = _parse_simple_yaml(txt)
        obj = {"enabled": True, "source": os.path.abspath(path), "constraints": constraints}
    except Exception as e:
        obj = {"enabled": False, "source": os.path.abspath(path), "error": str(e), "constraints": {}}
    _ANALOGPRO_CACHE["constraints"] = obj
    return obj


# Override guard-ring selection so constraints can participate.
_AP_OLD_CHOOSE_GUARD_RING_INSTANCES = choose_guard_ring_instances
def choose_guard_ring_instances(devices):
    chosen, report = _AP_OLD_CHOOSE_GUARD_RING_INSTANCES(devices)
    c = load_analogpro_constraints()
    cons = c.get("constraints", {}) if c else {}
    gr = cons.get("guard_rings") or cons.get("guard_ring") or []
    if not c.get("enabled") or not gr:
        return chosen, report
    fet_devices = [d for d in devices if is_regular_pr_mos(d.get("cell", ""))]
    all_set = {d["inst"].lower() for d in fet_devices}
    add = set()
    if isinstance(gr, str):
        gr = [gr]
    if isinstance(gr, list):
        for item in gr:
            if isinstance(item, str):
                if item.lower() in ("all", "*"):
                    add |= all_set
                else:
                    add.add(item.lower())
            elif isinstance(item, dict):
                val = item.get("instances") or item.get("insts") or item.get("devices") or item.get("device")
                if val == "all":
                    add |= all_set
                elif isinstance(val, list):
                    add |= {str(x).lower() for x in val}
                elif isinstance(val, str):
                    add.add(val.lower())
    chosen |= (add & all_set)
    report = dict(report or {})
    report["constraint_guard_rings"] = sorted(add & all_set)
    report["selected"] = sorted(chosen)
    report["guard_rings_enabled"] = bool(chosen)
    return chosen, report


def _entry_by_inst(entries):
    return {str(e["device"].get("inst")): e for e in entries}


def _constraint_groups_from_file(entries):
    c = load_analogpro_constraints()
    cons = c.get("constraints", {}) if c else {}
    by = _entry_by_inst(entries)
    groups = []
    for raw_key in ("mirror_groups", "matched_groups", "common_centroid_groups", "cc_groups"):
        for idx, g in enumerate(cons.get(raw_key, []) or []):
            if not isinstance(g, dict): continue
            insts = []
            ref = g.get("reference")
            if ref: insts.append(str(ref))
            for k in ("outputs", "devices", "instances", "members"):
                val = g.get(k)
                if isinstance(val, list):
                    insts.extend([str(x) for x in val])
                elif isinstance(val, str):
                    insts.extend([x.strip() for x in re.split(r"[,;\s]+", val) if x.strip()])
            insts = [x for x in dict.fromkeys(insts) if x in by]
            if len(insts) >= 2:
                groups.append({
                    "id": str(g.get("name") or g.get("id") or f"USER_CC_{idx+1}"),
                    "type": str(g.get("type") or raw_key),
                    "style": str(g.get("style") or CC_STYLE),
                    "reference": str(ref) if ref else insts[0],
                    "instances": insts,
                    "source": "constraints",
                    "dummies": bool(g.get("dummies", True)),
                    "guard": str(g.get("guard", g.get("guard_ring", "group_or_individual"))),
                    "raw": json_safe(g),
                })
    return groups


def _auto_common_centroid_groups(entries, topology_report=None, match_report=None):
    if not CC_AUTO_MIRRORS:
        return []
    by = _entry_by_inst(entries)
    out = []
    # Prefer topology current mirror groups because they include ratioed mirrors.
    if topology_report:
        for g in topology_report.get("groups", []):
            if g.get("type") in ("current_mirror", "ratioed_current_mirror") or "mirror" in str(g.get("type", "")):
                insts = [x for x in g.get("instances", []) if x in by]
                if len(insts) >= 2:
                    out.append({
                        "id": g.get("id") or ("AUTO_CC_" + str(len(out)+1)),
                        "type": "auto_current_mirror_common_centroid",
                        "style": CC_STYLE,
                        "reference": g.get("reference") or insts[0],
                        "instances": insts,
                        "gate_net": g.get("gate_net"),
                        "source": "auto_topology",
                        "dummies": True,
                        "guard": "individual_magic_guard_or_group_boundary",
                    })
    # Then generic matched groups.
    if match_report:
        for g in match_report.get("groups", []):
            insts = [x for x in g.get("instances", []) if x in by]
            if len(insts) >= 2 and not any(set(insts) <= set(h["instances"]) for h in out):
                out.append({
                    "id": g.get("id") or ("AUTO_MAT_" + str(len(out)+1)),
                    "type": "auto_matched_group_common_centroid",
                    "style": CC_STYLE,
                    "reference": insts[0],
                    "instances": insts,
                    "source": "auto_match_group",
                    "dummies": True,
                    "guard": "individual_magic_guard_or_group_boundary",
                })
    return out


def _units_for_entry_for_cc(entry):
    d = entry["device"]
    # Use unit array plan if already available.
    plan = entry.get("unit_array_plan") or d.get("unit_array_plan")
    if isinstance(plan, dict) and plan.get("units"):
        return int(plan.get("units"))
    w = d.get("w_um")
    if w is None:
        return 1
    try:
        return max(1, min(UNIT_ARRAY_MAX_UNITS, int(round(float(w) / max(UNIT_W_UM, 0.05)))))
    except Exception:
        return 1


def _cc_pattern_for_group(group, entries):
    by = _entry_by_inst(entries)
    insts = [i for i in group.get("instances", []) if i in by]
    counts = {i: _units_for_entry_for_cc(by[i]) for i in insts}
    tokens = []
    if len(insts) == 2:
        a, b = insts[0], insts[1]
        # ABBA with multiplicity balanced as much as possible.
        base = [a, b, b, a]
        reps = max(1, int(math.ceil(max(counts.values()) / 2.0)))
        tokens = (base * reps)[:max(4, sum(min(counts[i], 2*reps) for i in insts))]
    else:
        # Symmetric mirrored sequence: A B C ... C B A.  This is a centroid-friendly
        # ordering for planning/markers; actual unit-level routing remains manual.
        seq = insts
        tokens = seq + list(reversed(seq))
    # Add dummy placeholders at both edges as non-fab markers.
    if group.get("dummies", True) and DUMMY_MODE != "off":
        tokens = ["DUMMY"] * DUMMY_COUNT + tokens + ["DUMMY"] * DUMMY_COUNT
    return tokens, counts


def build_common_centroid_plan(entries, topology_report=None, match_report=None):
    if not ANALOGPRO_ENABLE or not CC_ENABLE or CC_MODE in ("off", "none", "0", "false"):
        return {"enabled": False, "groups": [], "mode": CC_MODE}
    user_groups = _constraint_groups_from_file(entries)
    auto_groups = _auto_common_centroid_groups(entries, topology_report, match_report)
    groups = user_groups[:]
    # Add auto groups that are not duplicate subsets of user groups.
    for g in auto_groups:
        s = set(g.get("instances", []))
        if not any(s <= set(u.get("instances", [])) for u in groups):
            groups.append(g)
    by = _entry_by_inst(entries)
    planned = []
    for g in groups:
        insts = [i for i in g.get("instances", []) if i in by]
        if len(insts) < 2:
            continue
        tokens, counts = _cc_pattern_for_group({**g, "instances": insts}, entries)
        # Geometry extents are filled later when positions exist.
        planned.append({
            "id": g.get("id"),
            "type": g.get("type"),
            "style": g.get("style", CC_STYLE),
            "source": g.get("source"),
            "reference": g.get("reference", insts[0]),
            "instances": insts,
            "unit_counts": counts,
            "pattern": tokens,
            "dummy_mode": DUMMY_MODE,
            "dummy_count_each_side": DUMMY_COUNT if g.get("dummies", True) else 0,
            "guard_strategy": g.get("guard", "individual_magic_guard_or_group_boundary"),
            "notes": [
                "This is a common-centroid/interdigitation placement plan. It uses unit-count abstraction from W/nf/m or unit-array plan.",
                "Dummy entries are markers by default so they do not alter LVS; set SKY_DUMMY_MODE=physical_lvs_risky only after manual review.",
                "Group guard rings are represented as planning boundaries; device-level Magic guard rings remain the DRC-safe physical default."
            ],
        })
    report = {
        "enabled": True,
        "mode": CC_MODE,
        "style": CC_STYLE,
        "groups": planned,
        "constraint_file": load_analogpro_constraints(),
    }
    _ANALOGPRO_CACHE["cc_plan"] = report
    return report


def _group_entries_from_plan(plan_group, entries):
    by = _entry_by_inst(entries)
    return [by[i] for i in plan_group.get("instances", []) if i in by]


def _apply_cc_marker_positions(positions, entries, cc_plan):
    """For marker/layout_hint mode, arrange each planned group in a clean symmetric row.

    This does not merge routed diffusion; it physically moves the detected/grouped
    device wrappers so the CC/matching intent is visible.  True unit-level merged
    diffusion is intentionally left as a plan because it can change LVS extraction.
    """
    if not cc_plan.get("enabled") or CC_MODE not in ("markers", "layout_hint", "layout", "plan"):
        return positions
    used = set()
    y_cursor = 0.0
    for g in cc_plan.get("groups", []):
        group_entries = _group_entries_from_plan(g, entries)
        if len(group_entries) < 2:
            continue
        # Only reposition if all entries have not already been claimed by a stronger group.
        fresh = [e for e in group_entries if id(e) not in used]
        if len(fresh) < 2:
            continue
        x = 0.0
        max_h = 0.0
        # Reference first/center-ish ordering: place reference, outputs, but mirror around center by sorting pattern instances.
        order = []
        for tok in g.get("pattern", []):
            if tok != "DUMMY" and tok not in order and tok in _entry_by_inst(entries):
                order.append(tok)
        if not order:
            order = g.get("instances", [])
        for inst in order:
            e = _entry_by_inst(entries).get(inst)
            if not e or id(e) in used:
                continue
            w, h = bbox_um(e)
            positions[id(e)] = (x, y_cursor)
            used.add(id(e))
            x += w + max(ANALOG_COL_GAP_UM, COMPACT_X_MARGIN_UM, 10.0)
            max_h = max(max_h, h)
        y_cursor -= max_h + max(ANALOG_ROW_GAP_UM, 18.0)
    return positions


_AP_OLD_COMPUTE_POSITIONS = compute_positions
def compute_positions(entries):
    # First run proven placement engine.
    positions, summary = _AP_OLD_COMPUTE_POSITIONS(entries)
    topology = analyze_circuit_topology(entries, {}) if ANALOGPRO_ENABLE else None
    match = detect_and_tag_matched_groups(entries) if ANALOGPRO_ENABLE else None
    cc_plan = build_common_centroid_plan(entries, topology, match)
    if CC_MARKERS and cc_plan.get("enabled") and CC_MODE in ("markers", "layout_hint", "layout"):
        positions = _apply_cc_marker_positions(positions, entries, cc_plan)
        summary = dict(summary)
        summary["common_centroid_layout_hint_applied"] = True
    summary = dict(summary)
    summary["analogpro"] = {
        "constraints": load_analogpro_constraints(),
        "common_centroid_groups": len(cc_plan.get("groups", [])) if cc_plan else 0,
        "cc_mode": CC_MODE,
        "template_mode": TEMPLATE_MODE,
    }
    _ANALOGPRO_CACHE["entries"] = entries
    _ANALOGPRO_CACHE["placement"] = {"positions": positions, "summary": summary}
    _ANALOGPRO_CACHE["cc_plan"] = cc_plan
    return positions, summary


def _bbox_for_group(entries):
    xs, ys = [], []
    for e in entries:
        x0, y0, x1, y1 = entry_bbox_extents_um(e)
        xs += [x0, x1]; ys += [y0, y1]
    if not xs: return None
    return min(xs), min(ys), max(xs), max(ys)


def draw_common_centroid_markers(layout, top, entries, cc_plan=None):
    if not (ANALOGPRO_ENABLE and CC_MARKERS and ADD_LABELS):
        return {"cc_markers": False}
    cc_plan = cc_plan or _ANALOGPRO_CACHE.get("cc_plan") or build_common_centroid_plan(entries)
    if not cc_plan.get("enabled"):
        return {"cc_markers": False, "reason": "cc disabled"}
    box_layer = layout.layer(*CC_BOX_LAYER)
    axis_layer = layout.layer(*CC_AXIS_LAYER)
    dummy_layer = layout.layer(*CC_DUMMY_LAYER)
    text_layer = layout.layer(*CC_TEXT_LAYER)
    markers = []
    by = _entry_by_inst(entries)
    for g in cc_plan.get("groups", []):
        ges = [by[i] for i in g.get("instances", []) if i in by]
        bb = _bbox_for_group(ges)
        if not bb:
            continue
        x0, y0, x1, y1 = bb
        pad = float(os.environ.get("SKY_CC_MARKER_PAD_UM", "3.0"))
        x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
        top.shapes(box_layer).insert(pya.Box(dbu_um(layout, x0), dbu_um(layout, y0), dbu_um(layout, x1), dbu_um(layout, y1)))
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        top.shapes(axis_layer).insert(pya.Path([pya.Point(dbu_um(layout, cx), dbu_um(layout, y0)), pya.Point(dbu_um(layout, cx), dbu_um(layout, y1))], max(1, dbu_um(layout, 0.08))))
        top.shapes(axis_layer).insert(pya.Path([pya.Point(dbu_um(layout, x0), dbu_um(layout, cy)), pya.Point(dbu_um(layout, x1), dbu_um(layout, cy))], max(1, dbu_um(layout, 0.08))))
        add_text(layout, top, text_layer, f"CC {g.get('id')} {g.get('style')}\npattern: {' '.join(map(str,g.get('pattern',[])[:24]))}", x0, y1 + 1.0, 0.55)
        # Dummy markers outside group edges by default: non-fab visual hints.
        if DUMMY_MODE != "off" and g.get("dummy_count_each_side", 0):
            dw = 0.7
            for k in range(int(g.get("dummy_count_each_side", 1))):
                off = (k + 1) * (dw + 0.5)
                top.shapes(dummy_layer).insert(pya.Box(dbu_um(layout, x0 - off), dbu_um(layout, y0), dbu_um(layout, x0 - off + dw), dbu_um(layout, y1)))
                top.shapes(dummy_layer).insert(pya.Box(dbu_um(layout, x1 + off - dw), dbu_um(layout, y0), dbu_um(layout, x1 + off), dbu_um(layout, y1)))
        markers.append({"id": g.get("id"), "bbox_um": [x0, y0, x1, y1], "axis": [cx, cy], "instances": g.get("instances", [])})
    return {"cc_markers": True, "groups_marked": len(markers), "markers": markers, "layers": {"box": CC_BOX_LAYER, "axis": CC_AXIS_LAYER, "dummy": CC_DUMMY_LAYER, "text": CC_TEXT_LAYER}}


def build_routing_plan(entries, net_stats=None, topology_report=None):
    if not ROUTING_PLAN_ENABLE:
        return {"enabled": False, "routes": []}
    net_stats = net_stats or build_net_statistics(entries)
    topology_report = topology_report or analyze_circuit_topology(entries, {})
    net_classes = topology_report.get("nets", {})
    routes = []
    for r in net_stats:
        net = r.get("net")
        nclass = (net_classes.get(net) or {}).get("classification", "")
        roles = set(r.get("roles", []))
        is_power = r.get("is_power")
        if is_power:
            layer_pref = "M1/M2 wide rail or strap"
            width = "wide, EM-aware"
            shield = "not needed; this is the shield/return reference"
            order = 10
        elif nclass in ("high_impedance_bias_or_gate_bus", "bias_or_feedback_sensitive") or "G" in roles:
            layer_pref = "M2 horizontal / M3 vertical preferred, avoid diffusion/poly jogs"
            width = "minimum ok for bias, but keep short; use shielding if long"
            shield = "shield with quiet 0/VSS or VDD if crossing noisy regions"
            order = 1
        elif "D" in roles or "S" in roles:
            layer_pref = "M1 local, M2 for longer branch routes"
            width = "normal; increase for current-carrying branches"
            shield = "avoid coupling into bias/reference nodes"
            order = 4
        else:
            layer_pref = "M1/M2 local"
            width = "normal"
            shield = "normal"
            order = 6
        routes.append({
            "net": net,
            "priority_order": order,
            "criticality": r.get("criticality", "normal"),
            "net_class": nclass,
            "pin_count": r.get("pin_count"),
            "hpwl_um": round(float(r.get("hpwl_um") or 0.0), 3),
            "recommended_layer": layer_pref,
            "recommended_width": width,
            "shielding": shield,
            "instances": r.get("instances", []),
            "roles": r.get("roles", []),
        })
    routes.sort(key=lambda x: (x["priority_order"], -int(x.get("pin_count") or 0), str(x["net"])))
    plan = {"enabled": True, "routes": routes, "notes": [
        "This is a routing plan, not metal autorouting.",
        "Bias/gate/reference nets are prioritized before local drains/sources.",
        "Power/body nets should be low resistance and tap-friendly."
    ]}
    _ANALOGPRO_CACHE["routing_plan"] = plan
    return plan


def write_routing_plan_outputs(out_gds, routing_plan):
    if not ROUTING_PLAN_ENABLE:
        return {"written": False}
    json_path = out_gds + ".routing_plan.json"
    csv_path = out_gds + ".routing_plan.csv"
    with open(json_path, "w") as f:
        json.dump(json_safe(routing_plan), f, indent=2)
    keys = ["net", "priority_order", "criticality", "net_class", "pin_count", "hpwl_um", "recommended_layer", "recommended_width", "shielding", "instances", "roles"]
    with open(csv_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=keys)
        wr.writeheader()
        for r in routing_plan.get("routes", []):
            rr = dict(r)
            rr["instances"] = ",".join(rr.get("instances", []))
            rr["roles"] = ",".join(rr.get("roles", []))
            wr.writerow({k: rr.get(k, "") for k in keys})
    return {"written": True, "json": json_path, "csv": csv_path, "routes": len(routing_plan.get("routes", []))}


def run_magic_extract_lvs_helper(gds_path, top_name):
    """Optional extraction/LVS helper.

    This is deliberately a helper/report, not a guarantee of LVS clean, because
    unit arrays/dummy devices may require netgen setup options to combine parallel
    MOS devices or ignore dummy devices.
    """
    if not LVS_CHECK:
        return {"lvs_check": False}
    if not shutil.which(SKY_MAGIC_BIN):
        return {"lvs_check": False, "reason": f"Magic executable not found: {SKY_MAGIC_BIN}"}
    if not os.path.isfile(SKY_MAGIC_RC):
        return {"lvs_check": False, "reason": f"Magic rcfile not found: {SKY_MAGIC_RC}"}
    workdir = tempfile.mkdtemp(prefix="sky130_lvs_extract_")
    tcl = os.path.join(workdir, "extract_lvs.tcl")
    ext_spice = os.path.join(workdir, f"{top_name}_extracted.spice")
    log_path = gds_path + ".magic_extract_lvs.log"
    tcl_text = f"""
gds read {tcl_brace(gds_path)}
load {tcl_brace(top_name)} -quiet
select top cell
expand
extract do local
extract all
ext2spice lvs
{'ext2spice blackbox on' if EXT2SPICE_BLACKBOX else ''}
ext2spice -o {tcl_brace(ext_spice)}
quit -noprompt
"""
    open(tcl, "w").write(tcl_text)
    env = os.environ.copy()
    env["MAGTYPE"] = "mag"
    try:
        proc = subprocess.run([SKY_MAGIC_BIN, "-dnull", "-noconsole", "-rcfile", SKY_MAGIC_RC, tcl],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              universal_newlines=True, cwd=workdir, env=env,
                              timeout=float(os.environ.get("SKY_LVS_TIMEOUT_S", "240")))
        with open(log_path, "w") as f:
            f.write("MAGIC EXTRACT STDOUT\n====================\n")
            f.write(proc.stdout or "")
            f.write("\n\nMAGIC EXTRACT STDERR\n====================\n")
            f.write(proc.stderr or "")
        result = {"lvs_check": True, "magic_extract_returncode": proc.returncode, "magic_log": log_path, "extracted_spice": ext_spice if os.path.isfile(ext_spice) else None, "workdir": workdir}
        # Optional netgen compare if SKY_NETLIST exists and netgen/setup exist.
        if shutil.which(NETGEN_BIN) and os.path.isfile(NETGEN_SETUP) and NETLIST and os.path.isfile(NETLIST) and os.path.isfile(ext_spice):
            netgen_log = gds_path + ".netgen_lvs.log"
            # General form; user can inspect log.  Subckt names may need setup per design.
            cmd = [NETGEN_BIN, "-batch", "lvs", NETLIST, ext_spice, NETGEN_SETUP]
            np = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=float(os.environ.get("SKY_NETGEN_TIMEOUT_S", "240")))
            with open(netgen_log, "w") as f:
                f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT\n======\n")
                f.write(np.stdout or "")
                f.write("\n\nSTDERR\n======\n")
                f.write(np.stderr or "")
            result["netgen_lvs"] = {"returncode": np.returncode, "log": netgen_log, "setup": NETGEN_SETUP}
        else:
            result["netgen_lvs"] = {"skipped": True, "reason": "netgen/setup/original/extracted netlist unavailable"}
        if os.environ.get("SKY_LVS_KEEP_WORKDIR", "1") != "1":
            shutil.rmtree(workdir, ignore_errors=True)
            result["workdir"] = None
        return result
    except Exception as e:
        return {"lvs_check": False, "reason": str(e), "workdir": workdir}


_AP_OLD_RUN_MAGIC_DRC = run_magic_drc_check_on_output
def run_magic_drc_check_on_output(gds_path, top_name):
    drc = _AP_OLD_RUN_MAGIC_DRC(gds_path, top_name)
    lvs = run_magic_extract_lvs_helper(gds_path, top_name)
    if isinstance(drc, dict):
        drc = dict(drc)
        drc["lvs_extract"] = lvs
        return drc
    return {"magic_drc": drc, "lvs_extract": lvs}


_AP_OLD_ADD_MATCH_GROUP_CARD = add_match_group_card
def add_match_group_card(layout, top, entries, match_report, map_report=None):
    # Draw existing card if requested, plus CC markers independent of card.
    old = _AP_OLD_ADD_MATCH_GROUP_CARD(layout, top, entries, match_report, map_report)
    cc = draw_common_centroid_markers(layout, top, entries, _ANALOGPRO_CACHE.get("cc_plan"))
    res = dict(old or {})
    res["common_centroid_markers"] = cc
    return res


_AP_OLD_WRITE_DEVICE_MAP_OUTPUTS = write_device_map_outputs
def write_device_map_outputs(out_gds, entries, net_stats=None, match_report=None):
    base = _AP_OLD_WRITE_DEVICE_MAP_OUTPUTS(out_gds, entries, net_stats, match_report)
    routing_plan = build_routing_plan(entries, net_stats, analyze_circuit_topology(entries, {}))
    routing_files = write_routing_plan_outputs(out_gds, routing_plan)
    base = dict(base or {})
    base["routing_plan"] = routing_files
    return base


_AP_OLD_WRITE_ANALOGSENSE_OUTPUTS = write_analogsense_outputs
def write_analogsense_outputs(out_gds, topology_report, warnings_report, net_stats=None):
    base = _AP_OLD_WRITE_ANALOGSENSE_OUTPUTS(out_gds, topology_report, warnings_report, net_stats)
    entries = _ANALOGPRO_CACHE.get("entries") or []
    cc_plan = _ANALOGPRO_CACHE.get("cc_plan") or build_common_centroid_plan(entries, topology_report, None)
    constraint_obj = load_analogpro_constraints()
    template_plan = {
        "enabled": ANALOGPRO_ENABLE,
        "template_mode": TEMPLATE_MODE,
        "templates": [],
        "notes": [
            "auto/bgr template keeps PMOS mirrors aligned, NMOS stacks column-oriented, and BJT/reference branch nearby.",
            "current mirror common-centroid plans are generated in common_centroid_plan.json.",
        ],
    }
    if TEMPLATE_MODE in ("auto", "bgr"):
        template_plan["templates"].append({"name": "bgr", "placement": "pmos_mirror_top__nmos_stacks_middle__bjt_reference_near_related_net", "status": "active_if_topology_matches"})
    if TEMPLATE_MODE in ("auto", "mirror"):
        template_plan["templates"].append({"name": "current_mirror", "placement": "aligned row plus common-centroid plan/markers", "status": "active_if_mirror_detected"})
    if TEMPLATE_MODE in ("auto", "stack"):
        template_plan["templates"].append({"name": "stack", "placement": "vertical source-drain columns", "status": "active_if_stack_detected"})
    dummy_plan = {
        "enabled": DUMMY_MODE != "off",
        "mode": DUMMY_MODE,
        "count_each_side": DUMMY_COUNT,
        "physical_dummy_warning": "Physical dummy MOS insertion is LVS-risky and disabled by default. Marker dummies are non-fab annotations.",
        "groups": [{"group": g.get("id"), "instances": g.get("instances"), "dummy_count_each_side": g.get("dummy_count_each_side", 0), "mode": DUMMY_MODE} for g in cc_plan.get("groups", [])],
    }
    paths = {
        "constraints_json": out_gds + ".constraints.normalized.json",
        "common_centroid_json": out_gds + ".common_centroid_plan.json",
        "dummy_plan_json": out_gds + ".dummy_plan.json",
        "layout_templates_json": out_gds + ".layout_templates.json",
    }
    open(paths["constraints_json"], "w").write(json.dumps(json_safe(constraint_obj), indent=2))
    open(paths["common_centroid_json"], "w").write(json.dumps(json_safe(cc_plan), indent=2))
    open(paths["dummy_plan_json"], "w").write(json.dumps(json_safe(dummy_plan), indent=2))
    open(paths["layout_templates_json"], "w").write(json.dumps(json_safe(template_plan), indent=2))
    base = dict(base or {})
    base["analogpro_files"] = paths
    base["common_centroid_groups"] = len(cc_plan.get("groups", []))
    base["dummy_groups"] = len(dummy_plan.get("groups", []))
    return base

# End AnalogPro v10 additions.


# -----------------------------------------------------------------------------
# AnalogProPlus v11: output bundle, terminal wizard, Magic-safe export, run docs
# -----------------------------------------------------------------------------

OUTPUT_MODE = os.environ.get("SKY_OUTPUT_MODE", "run_dir").strip().lower()  # run_dir|flat
RUN_ROOT = os.environ.get("SKY_RUN_ROOT", "").strip()
RUN_DIR = os.environ.get("SKY_RUN_DIR", "").strip()
RUN_NAME = os.environ.get("SKY_RUN_NAME", "").strip()
RUN_README = os.environ.get("SKY_RUN_README", "1") == "1"
RUN_DASHBOARD = os.environ.get("SKY_RUN_DASHBOARD", "1") == "1"
RUN_MANIFEST = os.environ.get("SKY_RUN_MANIFEST", "1") == "1"
RUN_COMMAND = os.environ.get("SKY_RUN_COMMAND", "").strip()
WIZARD_ENABLE = os.environ.get("SKY_WIZARD", "0").strip().lower() in ("1", "true", "yes", "on", "wizard")
MAGIC_SAFE_GDS = os.environ.get("SKY_MAGIC_SAFE_GDS", "1") == "1"
MAGIC_STRIP_DEBUG_LAYERS = os.environ.get("SKY_MAGIC_STRIP_DEBUG_LAYERS", "1") == "1"
MAGIC_SAFE_SUFFIX = os.environ.get("SKY_MAGIC_SAFE_SUFFIX", "_magic_safe")

_OUTPUT_BUNDLE_CACHE = {"run_dir": None, "requested_out": None}


def _plus_bool_prompt(prompt, default=False):
    default_txt = "Y/n" if default else "y/N"
    ans = input_or_default(f"{prompt} [{default_txt}]: ", "y" if default else "n").strip().lower()
    if ans in ("y", "yes", "1", "true", "on"):
        return True
    if ans in ("n", "no", "0", "false", "off", ""):
        return bool(default) if ans == "" else False
    return bool(default)


def _plus_choice_prompt(prompt, choices, default):
    choices = list(choices)
    ans = input_or_default(f"{prompt} ({'/'.join(choices)}) [default {default}]: ", default).strip().lower()
    return ans if ans in choices else default


def apply_analogpro_profile(profile):
    """Apply a compact user-facing profile by changing global knobs before generation."""
    global UNIT_ARRAY_MODE, CC_MODE, DUMMY_MODE, TEMPLATE_MODE, MAGIC_DRC_CHECK, LVS_CHECK, GUIDE_STYLE
    profile = str(profile or "").strip().lower()
    if profile in ("quick", "q"):
        UNIT_ARRAY_MODE = "plan"
        CC_MODE = "plan"
        DUMMY_MODE = "marker"
        TEMPLATE_MODE = "auto"
        GUIDE_STYLE = "off"
        MAGIC_DRC_CHECK = False
        LVS_CHECK = False
    elif profile in ("pro", "p", "default"):
        UNIT_ARRAY_MODE = "layout"
        CC_MODE = "markers"
        DUMMY_MODE = "marker"
        TEMPLATE_MODE = "auto"
        GUIDE_STYLE = "off"
        MAGIC_DRC_CHECK = False
        LVS_CHECK = False
    elif profile in ("verify", "v", "drc"):
        UNIT_ARRAY_MODE = "layout"
        CC_MODE = "markers"
        DUMMY_MODE = "marker"
        TEMPLATE_MODE = "auto"
        GUIDE_STYLE = "off"
        MAGIC_DRC_CHECK = True
        LVS_CHECK = True
    elif profile in ("clean", "c"):
        UNIT_ARRAY_MODE = "plan"
        CC_MODE = "plan"
        DUMMY_MODE = "marker"
        TEMPLATE_MODE = "auto"
        GUIDE_STYLE = "off"
        MAGIC_DRC_CHECK = True
        LVS_CHECK = False
    return profile


def maybe_run_terminal_wizard(devices=None, metadata=None):
    """Small terminal UI. It asks only high-impact questions, then uses env defaults for the rest."""
    if not WIZARD_ENABLE:
        profile = os.environ.get("SKY_PRESET", "").strip().lower()
        if profile:
            apply_analogpro_profile(profile)
        return {"wizard": False, "preset": profile or ""}

    print("\n========== AnalogProPlus Wizard ==========")
    print("This asks only the important choices. Everything else remains script-default.")

    profile = _plus_choice_prompt("Run profile", ["quick", "pro", "verify", "clean"], os.environ.get("SKY_PRESET", "pro").strip().lower() or "pro")
    apply_analogpro_profile(profile)

    mos_backend = _plus_choice_prompt(
        "MOS generation backend",
        ["magic", "gdsfactory", "hybrid"],
        os.environ.get("SKY_MOS_BACKEND", "hybrid").strip().lower() or "hybrid"
    )
    os.environ["SKY_MOS_BACKEND"] = mos_backend

    placement_style = _plus_choice_prompt(
        "Placement style",
        ["analogpro", "schematic"],
        os.environ.get("SKY_PLACEMENT_STYLE", PLACEMENT_STYLE).strip().lower() or "analogpro"
    )
    globals()["PLACEMENT_STYLE"] = placement_style
    os.environ["SKY_PLACEMENT_STYLE"] = placement_style

    print("Profiles: quick=reports only, pro=unit arrays+CC markers, verify=pro+DRC/LVS helper, clean=DRC-friendly planning.")
    print("MOS backend: magic=selection-safe Magic guard-ring generation, gdsfactory=direct draw, hybrid=selected Magic guard-ring + gdsfactory.")
    print("Placement: analogpro=topology-aware mirror/stack/reference placement, schematic=SPICE order with large MOS kept close in one row.")

    if _plus_bool_prompt("Use physical unit arrays for large MOS devices", UNIT_ARRAY_MODE == "layout"):
        globals()["UNIT_ARRAY_MODE"] = "layout"
    else:
        globals()["UNIT_ARRAY_MODE"] = "plan"

    globals()["CC_MODE"] = _plus_choice_prompt("Common-centroid mode", ["plan", "markers", "layout_hint", "off"], CC_MODE if CC_MODE in ("plan", "markers", "layout_hint", "off") else "markers")
    globals()["DUMMY_MODE"] = _plus_choice_prompt("Dummy mode", ["marker", "off"], DUMMY_MODE if DUMMY_MODE in ("marker", "off") else "marker")
    globals()["MAGIC_DRC_CHECK"] = _plus_bool_prompt("Run Magic DRC", MAGIC_DRC_CHECK)
    globals()["LVS_CHECK"] = _plus_bool_prompt("Run Magic extract/Netgen LVS helper", LVS_CHECK)

    rat_mode = _plus_choice_prompt(
        "Create separate ratline reference GDS",
        ["off", "points", "full", "both"],
        os.environ.get("SKY_RATLINE_GDS", RATLINE_GDS_MODE).strip().lower() or "off"
    )
    os.environ["SKY_RATLINE_GDS"] = rat_mode
    print("Ratline GDS: off=clean main GDS only, points=terminal markers, full=connected non-fab lines, both=points+full.")
    print("==========================================\n")

    return {
        "wizard": True,
        "profile": profile,
        "mos_backend": os.environ.get("SKY_MOS_BACKEND", "hybrid"),
        "placement_style": PLACEMENT_STYLE,
        "ratline_gds": os.environ.get("SKY_RATLINE_GDS", "off"),
        "unit_array_mode": UNIT_ARRAY_MODE,
        "cc_mode": CC_MODE,
        "dummy_mode": DUMMY_MODE,
        "magic_drc": MAGIC_DRC_CHECK,
        "lvs": LVS_CHECK,
    }


def setup_output_bundle_path(netlist_path, requested_out):
    """Move generated files into a clean run folder unless SKY_OUTPUT_MODE=flat.

    If user asks for OUT_GDS=foo.gds while in cwd, final GDS becomes:
        ./skyspice_runs/foo_YYYYMMDD_HHMMSS/foo.gds
    All existing report writers append to OUT_GDS, so every report lands in the same run folder.
    """
    global OUT_GDS
    if str(OUTPUT_MODE).lower() in ("flat", "legacy", "old", "0", "false", "off"):
        OUT_GDS = os.path.abspath(requested_out)
        _OUTPUT_BUNDLE_CACHE["run_dir"] = os.path.dirname(OUT_GDS)
        _OUTPUT_BUNDLE_CACHE["requested_out"] = requested_out
        return OUT_GDS
    requested_out = os.path.abspath(requested_out)
    gds_base = os.path.basename(requested_out)
    stem = os.path.splitext(gds_base)[0]
    safe_stem = sanitize_cell_name(RUN_NAME or stem).replace("$", "_").replace(".", "_")
    ts = time.strftime("%Y%m%d_%H%M%S")
    if RUN_DIR:
        run_dir = os.path.abspath(RUN_DIR)
    else:
        root = os.path.abspath(RUN_ROOT) if RUN_ROOT else os.path.join(os.getcwd(), "skyspice_runs")
        run_dir = os.path.join(root, f"{safe_stem}_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    OUT_GDS = os.path.join(run_dir, gds_base)
    _OUTPUT_BUNDLE_CACHE["run_dir"] = run_dir
    _OUTPUT_BUNDLE_CACHE["requested_out"] = requested_out
    return OUT_GDS


def _list_run_files(run_dir):
    rows = []
    if not run_dir or not os.path.isdir(run_dir):
        return rows
    for root, dirs, files in os.walk(run_dir):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, run_dir)
            try:
                size = os.path.getsize(p)
            except Exception:
                size = 0
            rows.append({"path": rel, "size_bytes": size})
    rows.sort(key=lambda x: x["path"])
    return rows


def _important_env_snapshot():
    keys = sorted([k for k in os.environ if k.startswith("SKY_") or k in ("PDK_ROOT", "PDK", "PDKPATH", "KLAYOUT_PY_SITE")])
    return {k: os.environ.get(k, "") for k in keys}


def write_constraints_template(run_dir):
    path = os.path.join(run_dir, "BGR_constraints_template.yaml")
    txt = """# AnalogProPlus constraint template
# Copy this file, edit, and run with: SKY_CONSTRAINTS=/path/to/file.yaml

mirror_groups:
  - name: PMOS_BIAS_MIRROR
    reference: X8
    outputs: [X10, X11, X12, Xa, Xd]
    style: common_centroid
    dummies: true
    guard: individual_magic_guard_or_group_boundary

stacks:
  - name: NMOS_REF_STACK
    devices: [X1, X2, X3]
    orientation: vertical

critical_nets:
  - net2
  - net03
  - net13
  - net19

guard_rings:
  - all
"""
    try:
        open(path, "w").write(txt)
    except Exception:
        return None
    return path


def write_rerun_script(run_dir, report):
    path = os.path.join(run_dir, "rerun.sh")
    out_gds = os.path.basename(report.get("out_gds", "output.gds"))
    netlist = report.get("netlist", "")
    env = _important_env_snapshot()
    # Keep it readable; include only flow-level knobs most users care about.
    keep = [
        "SKY_GUARD_RING", "SKY_GR_INSTS", "SKY_PLACE_MODE", "SKY_LAYOUT_STYLE",
        "SKY_GUIDE_STYLE", "SKY_UNIT_ARRAY_MODE", "SKY_UNIT_W_UM",
        "SKY_UNIT_ARRAY_MIN_W_UM", "SKY_CC_MODE", "SKY_DUMMY_MODE",
        "SKY_ANALOG_TEMPLATE", "SKY_MAGIC_DRC_CHECK", "SKY_LVS_CHECK",
        "SKY_RATLINE_GDS", "SKY_PLACEMENT_STYLE", "SKY_MAGIC_ALL_GUARD_RINGS",
        "SKY_SCHEMATIC_LARGE_ROW", "SKY_SCHEMATIC_LARGE_W_UM",
        "SKY_OUTPUT_MODE", "SKY_RUN_ROOT", "SKY_CONSTRAINTS"
    ]
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    lines.append("# Re-run from the original command directory or edit paths below.")
    for k in keep:
        v = env.get(k)
        if v:
            lines.append(f"export {k}={shlex_quote(v)}")
    lines.append("")
    lines.append(f"skyspice2klayout_all_magicgr_analogproplus {shlex_quote(netlist)} {shlex_quote(out_gds)}")
    try:
        open(path, "w").write("\n".join(lines) + "\n")
        os.chmod(path, 0o755)
    except Exception:
        return None
    return path


def shlex_quote(s):
    s = str(s)
    if re.match(r"^[A-Za-z0-9_./:=,+-]+$", s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def write_run_readme(run_dir, report, manifest_path=None):
    path = os.path.join(run_dir, "README_run.md")
    cc_count = len((((report.get("analogsense_files") or {}).get("analogpro_files") or {}) if False else []))
    txt = f"""# AnalogProPlus Run

Generated by `skyspice2klayout_all_magicgr_analogproplus`.

## Main files

- Layout: `{os.path.basename(report.get("out_gds", ""))}`
- Full JSON report: `{os.path.basename(report.get("out_gds", ""))}.report.json`
- Topology: `{os.path.basename(report.get("out_gds", ""))}.topology.json`
- Routing plan: `{os.path.basename(report.get("out_gds", ""))}.routing_plan.csv`
- Common-centroid plan: `{os.path.basename(report.get("out_gds", ""))}.common_centroid_plan.json`
- Unit arrays: `{os.path.basename(report.get("out_gds", ""))}.unit_arrays.csv`
- Warnings: `{os.path.basename(report.get("out_gds", ""))}.layout_warnings.csv`

## What to inspect first

1. Open the GDS in KLayout.
2. Check the common-centroid marker boundary and dummy marker regions.
3. Open `layout_warnings.csv`.
4. Open `routing_plan.csv`.
5. If DRC/LVS was enabled, inspect the Magic and Netgen logs.

## Important note on dummy devices and common-centroid

Default dummy devices are **non-fab markers**. They are intentionally not real MOS devices, because real dummy insertion requires final source/drain/gate/body tie decisions and LVS handling. Device-level Magic guard rings remain the safest physical guard-ring implementation. Group guard-ring boxes are planning boundaries unless you manually convert them to a process-correct tap/well ring.

## Re-run

Use `rerun.sh` as a starting point.
"""
    try:
        open(path, "w").write(txt)
    except Exception:
        return None
    return path


def write_html_dashboard(run_dir, report):
    path = os.path.join(run_dir, "index.html")
    files = _list_run_files(run_dir)
    warnings = ((report.get("layout_warnings") or {}).get("warnings") or [])[:30]
    routes = (report.get("routing_priority") or [])[:20]
    cc_file = (report.get("analogsense_files") or {}).get("analogpro_files", {}).get("common_centroid_json", "")
    gds_name = os.path.basename(report.get("out_gds", ""))
    def esc(x):
        return str(x).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    file_rows = "\n".join([f"<tr><td><a href='{esc(f['path'])}'>{esc(f['path'])}</a></td><td>{f['size_bytes']}</td></tr>" for f in files])
    warn_rows = "\n".join([f"<tr><td>{esc(w.get('severity',''))}</td><td>{esc(w.get('inst',''))}</td><td>{esc(w.get('category',''))}</td><td>{esc(w.get('message',''))}</td></tr>" for w in warnings])
    route_rows = "\n".join([f"<tr><td>{esc(r.get('net',''))}</td><td>{esc(r.get('criticality',''))}</td><td>{esc(r.get('net_class',''))}</td><td>{esc(r.get('recommendation',''))}</td></tr>" for r in routes])
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AnalogProPlus Run</title>
<style>
body{{font-family:Arial,sans-serif;margin:28px;line-height:1.35;background:#fafafa;color:#202124}}
h1,h2{{color:#111}} table{{border-collapse:collapse;width:100%;background:white;margin:12px 0}}
td,th{{border:1px solid #ddd;padding:6px;font-size:13px;vertical-align:top}} th{{background:#eee}}
.card{{background:white;border:1px solid #ddd;padding:12px;margin:12px 0;border-radius:8px}}
code{{background:#eee;padding:2px 4px;border-radius:4px}}
</style></head><body>
<h1>AnalogProPlus Run</h1>
<div class="card">
<b>GDS:</b> <code>{esc(gds_name)}</code><br>
<b>Placed:</b> {esc(report.get('placed_total'))} &nbsp; <b>Missing:</b> {esc(report.get('missing_total'))}<br>
<b>Version:</b> {esc(report.get('version'))}<br>
<b>Magic DRC:</b> {esc((report.get('magic_drc') or {}).get('magic_drc_check'))}
</div>
<h2>Warnings</h2>
<table><tr><th>Severity</th><th>Inst</th><th>Category</th><th>Message</th></tr>{warn_rows}</table>
<h2>Routing priority</h2>
<table><tr><th>Net</th><th>Criticality</th><th>Class</th><th>Recommendation</th></tr>{route_rows}</table>
<h2>Files</h2>
<table><tr><th>Path</th><th>Size bytes</th></tr>{file_rows}</table>
</body></html>"""
    try:
        open(path, "w").write(html)
    except Exception:
        return None
    return path


def write_output_bundle_files(report, report_path):
    run_dir = os.path.dirname(os.path.abspath(report_path))
    bundle = {"enabled": True, "run_dir": run_dir}
    if not os.path.isdir(run_dir):
        return {"enabled": False, "reason": "run_dir_missing", "run_dir": run_dir}
    if RUN_MANIFEST:
        manifest_path = os.path.join(run_dir, "manifest.json")
        manifest = {
            "run_dir": run_dir,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": report.get("version"),
            "netlist": report.get("netlist"),
            "out_gds": report.get("out_gds"),
            "top": report.get("top"),
            "environment": _important_env_snapshot(),
            "files": _list_run_files(run_dir),
        }
        try:
            open(manifest_path, "w").write(json.dumps(json_safe(manifest), indent=2))
            bundle["manifest"] = manifest_path
        except Exception as e:
            bundle["manifest_error"] = str(e)
    tmpl = write_constraints_template(run_dir)
    if tmpl: bundle["constraints_template"] = tmpl
    rerun = write_rerun_script(run_dir, report)
    if rerun: bundle["rerun_script"] = rerun
    if RUN_README:
        rd = write_run_readme(run_dir, report, bundle.get("manifest"))
        if rd: bundle["readme"] = rd
    if RUN_DASHBOARD:
        dash = write_html_dashboard(run_dir, report)
        if dash: bundle["dashboard"] = dash
    bundle["files_count"] = len(_list_run_files(run_dir))
    return json_safe(bundle)


def _clear_debug_layers_from_layout(layout_obj):
    try:
        layer_indices = list(layout_obj.layer_indices())
    except Exception:
        return {"cleared": 0, "error": "layer_indices_unavailable"}
    cleared = 0
    for li in layer_indices:
        try:
            info = layout_obj.get_info(li)
            lay = int(info.layer)
            dt = int(info.datatype)
        except Exception:
            continue
        # Strip annotation/debug layers only. Keep all SKY130 fab layers.
        if MAGIC_STRIP_DEBUG_LAYERS and (lay >= 250 or lay == 255):
            try:
                layout_obj.clear_layer(li)
                cleared += 1
            except Exception:
                pass
    return {"cleared": cleared}


def make_magic_safe_gds(gds_path):
    if not MAGIC_SAFE_GDS:
        return {"enabled": False, "path": gds_path}
    safe_path = os.path.splitext(gds_path)[0] + MAGIC_SAFE_SUFFIX + ".gds"
    try:
        ly = pya.Layout()
        ly.read(gds_path)
        clear_report = _clear_debug_layers_from_layout(ly)
        ly.write(safe_path)
        return {"enabled": True, "path": safe_path, "original": gds_path, "clear_report": clear_report}
    except Exception as e:
        return {"enabled": False, "path": gds_path, "error": str(e)}


# Add a final DRC/LVS wrapper that uses Magic-safe GDS with debug layers removed.
_AP_PLUS_OLD_DRC_LVS = run_magic_drc_check_on_output
def run_magic_drc_check_on_output(gds_path, top_name):
    safe = make_magic_safe_gds(gds_path) if (MAGIC_DRC_CHECK or LVS_CHECK) else {"enabled": False, "path": gds_path}
    use_gds = safe.get("path") or gds_path
    res = _AP_PLUS_OLD_DRC_LVS(use_gds, top_name)
    if isinstance(res, dict):
        res = dict(res)
        res["magic_safe_gds"] = safe
        res["original_gds"] = gds_path
        return res
    return {"result": res, "magic_safe_gds": safe, "original_gds": gds_path}


# End AnalogProPlus v11 additions.


try:
    main()
except Exception as e:
    print("\nERROR:", e)
    traceback.print_exc()
    raise
