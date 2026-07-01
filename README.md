# SKYSPICE2KLAYOUT AnalogProPlus v12

A hobby SKY130 SPICE-to-KLayout helper flow for analog layout exploration.

I built this as a summer hobby project, with AI assistance, because I was frustrated with the usual `import spice` experience while playing with SKY130 analog circuits. I wanted a cleaner starting point inside KLayout: parse a SPICE/CDL netlist, generate or import the devices, place them in a useful way, write reports, and give me enough visual context to continue manual layout work.

This is not a commercial EDA tool, not a signoff flow, and not a replacement for analog layout skill. I still treat manual review, Magic DRC, extraction, LVS, and foundry rule checks as the real checks. This project is a playground for making SPICE-to-GDS exploration less painful.

## Contents

- [Project scope](#project-scope)
- [Internal flow](#internal-flow)
- [Analog intuition behind the flow](#analog-intuition-behind-the-flow)
- [Why hybrid mode exists](#why-hybrid-mode-exists)
- [Repository layout](#repository-layout)
- [File map](#file-map)
- [How the device-generation files are used](#how-the-device-generation-files-are-used)
- [Using the copied helper files safely](#using-the-copied-helper-files-safely)
- [Expected paths](#expected-paths)
- [Why I use klayout_gf7](#why-i-use-klayout_gf7)
- [Setup](#setup)
- [Doctor check](#doctor-check)
- [Command syntax](#command-syntax)
- [Run folder behavior](#run-folder-behavior)
- [Run modes](#run-modes)
- [Wizard mode](#wizard-mode)
- [Command examples](#command-examples)
- [Generated outputs](#generated-outputs)
- [Troubleshooting](#troubleshooting)
- [Validation order](#validation-order)
- [Final note](#final-note)
- For a detailed visual walkthrough with KLayout screenshots, terminal logs, ratline views, schematic-oriented placement, and wizard-mode output, see the [`showcase-screenshots`](https://github.com/kafkayash/AnalogProV12_Spice_to_KLayoutGDS_Generator/tree/showcase-screenshots) branch.

## Project scope

The flow is meant to create a useful first layout view from a SKY130 SPICE/CDL netlist. It is not meant to finish analog layout automatically.

| Area | What v12 does |
| --- | --- |
| Netlist input | Parses SKY130 SPICE/CDL-style netlists and extracts devices, nodes, model names, and common parameters. |
| Device detection | Classifies MOS, BJT, diode, capacitor, resistor, standard-cell-like, IO-like, and fixed-device entries. |
| MOS generation | Supports `gdsfactory`, `magic`, and `hybrid` backends. |
| Fixed devices | Imports fixed GDS cells such as BJTs and other primitive SKY130 cells when available. |
| Placement | Supports `analogpro` topology-aware placement and `schematic` SPICE-order placement. |
| Reports | Writes device maps, net maps, topology reports, routing-priority reports, warnings, unit-array reports, and run dashboards. |
| Analog helpers | Adds unit-array planning, common-centroid markers, dummy markers, topology hints, and routing-intent reports. |
| Ratline reference | Can generate separate `_ratpoints.gds` and `_ratlines_full.gds` files on non-fab/debug layers. |
| Output handling | Keeps every run in a timestamped folder under `./skyspice_runs/`. |
| User interface | Supports command-line mode and interactive wizard mode. |

### What this project is not

| Not this | What I mean |
| --- | --- |
| Not a production analog layout generator | It gives a structured starting point. It does not create final analog layout. |
| Not a router | Ratlines are visual reference geometry, not routed metal. |
| Not a signoff tool | Magic DRC and LVS helpers are useful smoke checks, but final verification is still manual and tool-chain dependent. |
| Not a foundry-rule replacement | PDK rules, manual layout review, DRC, extraction, and LVS still matter. |
| Not a universal SKY130 installer | It assumes SKY130 PDK, KLayout, Magic, Netgen, and the gf7 Python environment are installed correctly. |

## Internal flow

At a high level, the tool runs like this:

```text
SPICE/CDL netlist
  -> launcher sets PDK and Python paths
  -> KLayout batch mode loads the Python script
  -> script parses devices and nets
  -> devices are generated or imported through Magic, gdsfactory, fixed GDS, or libs.ref fallback
  -> placement is created using analogpro or schematic mode
  -> optional helpers add unit-array planning, markers, ratline reference files, DRC/LVS helper logs
  -> all outputs are written into a clean timestamped run folder
```

| Stage | What happens | Why it matters |
|---|---|---|
| Launcher setup | Sets `PDKPATH`, `KLAYOUT_PATH`, `KLAYOUT_PY_SITE`, `PYTHONPATH`, and command options. | Most failures came from wrong paths, so the launcher is part of the flow, not just a wrapper. |
| Netlist parsing | Reads SPICE/CDL devices, instance names, nodes, W/L/nf/m values, and model names. | This decides what needs to be generated, imported, or reported as missing. |
| Device generation | Uses Magic, gdsfactory, fixed GDS, or library fallback depending on backend and device type. | Different device types need different strategies. Guard-ring MOS and normal MOS are not treated the same. |
| Placement | Uses `analogpro` or `schematic` mode. | I can either get a topology-aware starting point or a simple netlist-order view. |
| Reports | Writes CSV/JSON reports, warnings, routing priority, unit-array plans, and topology summaries. | The reports make the run debuggable instead of only producing a GDS. |
| Optional views | Ratpoints and ratlines are written as separate GDS files. | I can inspect connectivity without polluting the clean main GDS. |

## Analog intuition behind the flow

Analog layout is not only about placing shapes. Matching, symmetry, common-centroid thinking, dummy environment, body ties, guard rings, routing parasitics, device orientation, and local neighborhood all affect the final circuit. This script does not solve all of that, but it tries to organize the first messy step so the layout review starts from something understandable.

| Analog idea | How I represent it in the tool | Important limitation |
| --- | --- | --- |
| Large MOS devices often need unit thinking | `SKY_UNIT_ARRAY_MODE=plan` writes unit-array plans. `layout` mode can replace large devices with unit-array wrappers. | Strict LVS may see many parallel MOS devices instead of one large MOS. |
| Current mirrors should be visible early | Topology reports and placement hints try to group mirror-like devices. | This is a hint, not a final matched layout. |
| Sensitive nets should be obvious | Routing-priority reports flag important nets and estimated connectivity pressure. | It does not route the net. |
| Common-centroid starts with grouping | `SKY_CC_MODE=markers` adds non-fab planning markers and writes a plan file. | The marker is not a finished common-centroid layout. |
| Dummy devices should not be blindly inserted | `SKY_DUMMY_MODE=marker` adds dummy planning markers only. | Real dummy MOS insertion affects LVS and needs connection strategy. |
| Guard rings are real physical layout | Selected guard-ring devices are generated through Magic in hybrid mode. | Blind guard-ring generation can create DRC or spacing problems. |
| Sometimes schematic order is easier | `--placement schematic` follows the netlist order more directly. | It is simpler, not smarter. |
| Sometimes topology-aware placement helps | `--placement analogpro` tries analog grouping and layout hints. | It is still a starting point, not final placement. |

The practical goal is simple: the generated GDS should help me think about the analog layout, not pretend that layout is already complete.

## Why hybrid mode exists

This part matters because it came directly from the problems I hit while testing.

When I tried using the native SKY130 KLayout Python `draw_fet.py` guard-ring path for all guard-ring devices, I ran into DRC-style problems around guard-ring geometry, tap/well behavior, and device spacing. I did not want generated guard rings if they were going to create new layout problems.

Magic already has a SKY130 generation/import-style flow that is closer to what Magic itself expects. So my practical split became:

```text
selected MOS devices needing guard rings -> Magic backend
normal MOS devices without guard rings    -> gdsfactory / SKY130 Python draw files
fixed devices like BJT                    -> fixed GDS import
```

That is why `hybrid` exists. I do not treat one backend as always better. I use Magic where guard-ring behavior matters and gdsfactory/SKY130 Python helper cells where normal MOS direct generation is enough.

## Repository layout

This is the actual repository layout I am using now:

```text
AnalogProV12_Spice_to_KLayoutGDS_Generator/
├── README.md
├── analog_py_draw_klayout/
│   ├── fixed_devices/
│   │   ├── VPP/
│   │   ├── bjt/
│   │   ├── photodiode/
│   │   └── rf/
│   ├── __init__.py
│   ├── bjt.py
│   ├── cap.py
│   ├── diode.py
│   ├── draw_bjt.py
│   ├── draw_cap.py
│   ├── draw_diode.py
│   ├── draw_fet.py
│   ├── draw_guard_ring.py
│   ├── draw_rf.py
│   ├── draw_vpp.py
│   ├── fet.py
│   └── globals.py
├── examples/
│   └── BGR_sky130_ckt.spice
├── required_docs/
│   ├── AnalogProPlus_v12_Official_User_Guide.docx
│   ├── AnalogProPlus_v12_Official_User_Guide.pdf
│   └── README_AnalogProPlus_v12_official.md
├── required_scripts/
│   ├── skyspice2klayout_all_devices_magicgr_analogproplus.py
│   └── skyspice2klayout_all_magicgr_analogproplus
└── skyspice_runs/
```

I use the repository as a reproducible project bundle. The actual terminal install still copies the main script and launcher into these local folders:

```text
~/ASIC_eda/klayout_scripts/
~/ASIC_eda/bin/
```

The repository contains the project files, example netlist, documentation, and the helper draw files I used while testing. The generated run outputs go into `skyspice_runs/`.

I normally do not treat generated runs as source code. They are useful for testing and screenshots, but they can become large because every run can create GDS, JSON, CSV, HTML, and log files.

## File map

| Path | What it is | Why I keep it |
| --- | --- | --- |
| [`README.md`](README.md) | Main GitHub README. | This is the first file people see, so I keep setup, usage, and troubleshooting here. |
| [`required_scripts/skyspice2klayout_all_devices_magicgr_analogproplus.py`](required_scripts/skyspice2klayout_all_devices_magicgr_analogproplus.py) | Main KLayout Python script. | This is the actual SPICE-to-layout helper script. It parses the netlist, detects devices, generates/imports geometry, places devices, and writes reports. |
| [`required_scripts/skyspice2klayout_all_magicgr_analogproplus`](required_scripts/skyspice2klayout_all_magicgr_analogproplus) | Bash launcher. | This sets up the PDK paths, forces the `~/klayout_gf7` Python environment, handles CLI arguments, and runs KLayout in batch mode. |
| [`analog_py_draw_klayout/`](analog_py_draw_klayout/) | SKY130 Python draw/helper files used for compatibility and reference. | These files show the helper draw environment I tested against. They are useful if a local PDK install is missing or has incompatible helper files. |
| [`analog_py_draw_klayout/draw_fet.py`](analog_py_draw_klayout/draw_fet.py) | MOS draw helper. | This is relevant for the `gdsfactory` backend, where normal MOS devices are generated through Python helper geometry. |
| [`analog_py_draw_klayout/draw_guard_ring.py`](analog_py_draw_klayout/draw_guard_ring.py) | Guard-ring helper reference. | I keep this for reference, but my normal guard-ring flow uses Magic for selected MOS devices because that behaved better during testing. |
| [`analog_py_draw_klayout/draw_bjt.py`](analog_py_draw_klayout/draw_bjt.py) | BJT helper reference. | Useful for understanding SKY130 BJT helper behavior. |
| [`analog_py_draw_klayout/draw_cap.py`](analog_py_draw_klayout/draw_cap.py) | Capacitor helper reference. | Useful for capacitor helper generation/reference. |
| [`analog_py_draw_klayout/draw_diode.py`](analog_py_draw_klayout/draw_diode.py) | Diode helper reference. | Useful for diode helper generation/reference. |
| [`analog_py_draw_klayout/fixed_devices/`](analog_py_draw_klayout/fixed_devices/) | Fixed-device GDS folders. | Some devices are safer to import as fixed GDS cells instead of regenerating them procedurally. |
| [`examples/BGR_sky130_ckt.spice`](examples/BGR_sky130_ckt.spice) | Example SPICE netlist. | This is the bandgap-reference style test netlist I used while building and debugging the flow. |
| [`required_docs/AnalogProPlus_v12_Official_User_Guide.pdf`](required_docs/AnalogProPlus_v12_Official_User_Guide.pdf) | PDF guide. | Offline documentation with setup, commands, modes, and troubleshooting. |
| [`required_docs/AnalogProPlus_v12_Official_User_Guide.docx`](required_docs/AnalogProPlus_v12_Official_User_Guide.docx) | Editable guide. | Useful if I want to update the documentation later. |
| [`required_docs/README_AnalogProPlus_v12_official.md`](required_docs/README_AnalogProPlus_v12_official.md) | Documentation copy of the README. | Kept as a versioned documentation copy. |
| [`skyspice_runs/`](skyspice_runs/) | Generated run-output folder. | Every run creates a timestamped folder here. This is where GDS, reports, dashboards, logs, and rerun files are written. |

## How the device-generation files are used

This flow is not built around only one layout-generation method. I use a mixed strategy because different SKY130 device types behaved better through different paths.

| Source | Used for | Why I use it |
| --- | --- | --- |
| Magic SKY130 generation | Selected guard-ring MOS devices. | During testing, Magic-generated guard-ring MOS devices behaved more reliably than the KLayout Python guard-ring draw path. |
| SKY130 Python/gdsfactory draw helpers | Normal MOS and other drawable devices. | This is useful for fast direct generation inside the KLayout/Python flow. |
| Fixed-device GDS import | BJTs and other fixed primitives. | Some SKY130 devices already exist as fixed GDS cells, so importing them is cleaner than regenerating them. |
| `libs.ref` fallback | Library/reference cells. | If a cell is not generated directly, the script can search available reference GDS libraries. |
| Reports and debug layers | Net maps, device maps, ratlines, topology hints, and warnings. | These make the run understandable instead of only producing a raw GDS. |

This is also why `hybrid` mode exists.

```text
selected MOS devices needing guard rings -> Magic backend
normal MOS devices without guard rings    -> gdsfactory / SKY130 Python draw files
fixed devices like BJT                    -> fixed GDS import
```

I do not treat Magic or gdsfactory as universally better. I use each where it made the most practical sense during testing.

### Why I kept `analog_py_draw_klayout/`

The folder [`analog_py_draw_klayout/`](analog_py_draw_klayout/) is included so the helper environment is visible and reproducible. It also helps when comparing behavior between:

```text
local PDK helper files
copied helper files in this repository
Magic-generated guard-ring devices
fixed-device GDS imports
```

My normal preference is still to use the installed SKY130 PDK helper files first:

```text
/usr/local/share/pdk/sky130A/libs.tech/klayout/python/cells
/usr/local/share/pdk/sky130A/libs.tech/klayout/pymacros/cells
```

The copied helper files are mainly for reference, debugging, and reproducibility.

## Using the copied helper files safely

The normal source for SKY130 helper files should be the installed PDK:

```text
/usr/local/share/pdk/sky130A/libs.tech/klayout/python/cells
/usr/local/share/pdk/sky130A/libs.tech/klayout/pymacros/cells
```

If those files are missing or broken in a local install, the copied files in [`analog_py_draw_klayout/`](analog_py_draw_klayout/) can be used as a reference or copied into the PDK helper folder.

I do not recommend overwriting a working PDK folder randomly. Back it up first.

```bash
PDKPATH=/usr/local/share/pdk/sky130A

sudo cp -a "$PDKPATH/libs.tech/klayout/python/cells" \
  "$PDKPATH/libs.tech/klayout/python/cells.backup_$(date +%Y%m%d_%H%M%S)"
```

If copying is actually needed:

```bash
sudo cp -r analog_py_draw_klayout/* \
  /usr/local/share/pdk/sky130A/libs.tech/klayout/python/cells/
```

After copying, run the doctor check again:

```bash
skyspice2klayout_all_magicgr_analogproplus --doctor
```

Then test the gdsfactory backend first:

```bash
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend gdsfactory \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_gdsfactory_fixed.gds
```

If the current installed PDK helper files already work, I leave them alone.

## Expected paths

My working setup uses this path layout:

| Item | Expected path |
| --- | --- |
| PDK root | `/usr/local/share/pdk` |
| SKY130 PDK | `/usr/local/share/pdk/sky130A` |
| KLayout tech | `/usr/local/share/pdk/sky130A/libs.tech/klayout` |
| KLayout Python helpers | `/usr/local/share/pdk/sky130A/libs.tech/klayout/python` |
| KLayout pymacros helpers | `/usr/local/share/pdk/sky130A/libs.tech/klayout/pymacros` |
| Fixed-device GDS | `/usr/local/share/pdk/sky130A/libs.tech/klayout/python/cells/fixed_devices` |
| Magic rcfile | `/usr/local/share/pdk/sky130A/libs.tech/magic/sky130A.magicrc` |
| libs.ref | `/usr/local/share/pdk/sky130A/libs.ref` |
| gdsfactory venv | `~/klayout_gf7` |

If your PDK is somewhere else, update environment variables first:

```bash
export PDK_ROOT=/your/pdk/root
export PDK=sky130A
export PDKPATH="$PDK_ROOT/$PDK"
```

Then check the setup:

```bash
skyspice2klayout_all_magicgr_analogproplus --doctor
```

If needed, update only the launcher defaults:

```bash
nano ~/ASIC_eda/bin/skyspice2klayout_all_magicgr_analogproplus
```

Look for:

```bash
PDK_ROOT="${PDK_ROOT:-/usr/local/share/pdk}"
PDK="${PDK:-sky130A}"
PDKPATH="${PDKPATH:-$PDK_ROOT/$PDK}"
```

Change the default path only if your install is not under `/usr/local/share/pdk`.

## Why I use klayout_gf7

The gdsfactory side of this flow is sensitive to the gdsfactory/KFactory API version. I hit errors like:

```text
cellname dev_temp already exists
Cell object has no attribute add_array
Component.add_ref() got unexpected keyword argument spacing
list object has no attribute move
```

Those errors came from version/API mismatches between older SKY130 helper scripts and newer gdsfactory/KFactory behavior.

The stable solution I use is a separate virtual environment in my home folder:

```text
~/klayout_gf7
```

The launcher forces KLayout to use this venv instead of accidentally loading packages from:

```text
~/.local/lib/python3.12/site-packages
```

That `.local` fallback caused a lot of confusing behavior, so the launcher blocks user-site packages and forces the gf7 site-packages path.

## Setup

### 1. Create the klayout_gf7 environment

Run this once:

```bash
python3 -m venv ~/klayout_gf7
~/klayout_gf7/bin/python -m pip install --upgrade pip setuptools wheel
~/klayout_gf7/bin/pip install "gdsfactory==7.*" gdstk shapely numpy scipy pyyaml
```

Verify it directly:

```bash
~/klayout_gf7/bin/python - <<'PY'
import sys
print("python:", sys.executable)
import gdsfactory as gf
print("gdsfactory:", gf.__version__)
print("gdsfactory file:", gf.__file__)
import gdstk
print("gdstk:", gdstk.__version__)
PY
```

Expected idea:

```text
python: /home/<user>/klayout_gf7/bin/python
gdsfactory: 7.x.x
gdsfactory file: /home/<user>/klayout_gf7/lib/python3.12/site-packages/...
```

If this shows packages from `~/.local`, fix the environment before running the layout flow.

### 2. Install the script and launcher

From the project root:

```bash
mkdir -p ~/ASIC_eda/klayout_scripts ~/ASIC_eda/bin

cp required_scripts/skyspice2klayout_all_devices_magicgr_analogproplus.py \
   ~/ASIC_eda/klayout_scripts/skyspice2klayout_all_devices_magicgr_analogproplus.py

cp required_scripts/skyspice2klayout_all_magicgr_analogproplus \
   ~/ASIC_eda/bin/skyspice2klayout_all_magicgr_analogproplus

chmod +x ~/ASIC_eda/bin/skyspice2klayout_all_magicgr_analogproplus
```

Add the launcher folder to the shell path:

```bash
export PATH="$HOME/ASIC_eda/bin:$PATH"
hash -r
```

Optional permanent shell setup:

```bash
echo 'export PATH="$HOME/ASIC_eda/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Check which launcher is being used:

```bash
type -a skyspice2klayout_all_magicgr_analogproplus
```

The first result should be:

```text
/home/<user>/ASIC_eda/bin/skyspice2klayout_all_magicgr_analogproplus
```

### 3. Launcher path block

The launcher should force the gf7 environment. The important part should look like this:

```bash
GF7_PY="$HOME/klayout_gf7/bin/python"

KLAYOUT_PY_SITE="$($GF7_PY - <<'PY'
import site
print(site.getsitepackages()[0])
PY
)"

export KLAYOUT_PY_SITE
export PYTHONNOUSERSITE=1
export KLAYOUT_PATH="$PDKPATH/libs.tech/klayout"
export PYTHONPATH="$KLAYOUT_PY_SITE:$PDKPATH/libs.tech/klayout/python:$PDKPATH/libs.tech/klayout/pymacros:${PYTHONPATH:-}"
```

This block is important. It keeps KLayout from loading the wrong gdsfactory installation.

### 4. Optional helper draw files

The normal source for SKY130 helper files should be the installed PDK:

```text
/usr/local/share/pdk/sky130A/libs.tech/klayout/python/cells
/usr/local/share/pdk/sky130A/libs.tech/klayout/pymacros/cells
```

If those are missing or broken, I keep optional known-working helper draw files in:

```text
required_scripts/optional_sky130_helper_draw_files/
```

Back up the PDK helper folder before copying anything:

```bash
PDKPATH=/usr/local/share/pdk/sky130A

sudo cp -a "$PDKPATH/libs.tech/klayout/python/cells" \
           "$PDKPATH/libs.tech/klayout/python/cells.backup_$(date +%Y%m%d_%H%M%S)"
```

Copy only if needed:

```bash
sudo cp -r required_scripts/optional_sky130_helper_draw_files/cells/* \
   /usr/local/share/pdk/sky130A/libs.tech/klayout/python/cells/
```

If the current setup works, I do not touch this.

## Doctor check

Always run this first:

```bash
skyspice2klayout_all_magicgr_analogproplus --doctor
```

Expected important lines:

```text
KLAYOUT_PATH      : /usr/local/share/pdk/sky130A/libs.tech/klayout
KLAYOUT_PY_SITE   : /home/<user>/klayout_gf7/lib/python3.12/site-packages
PDKPATH           : /usr/local/share/pdk/sky130A
OK klayout
OK magic
OK netgen
OK PDKPATH exists
OK Magic rcfile
OK gdsfactory
```

| Doctor item | What I check |
| --- | --- |
| `SCRIPT` | Python script path loaded by KLayout batch mode. |
| `KLAYOUT_PATH` | SKY130 KLayout technology path. |
| `KLAYOUT_PY_SITE` | Must point to `~/klayout_gf7`, not `~/.local`. |
| `PDKPATH` | SKY130 PDK directory. |
| `klayout` | KLayout binary is available. |
| `magic` | Magic binary is available. |
| `netgen` | Netgen binary is available for optional LVS helper. |
| `gdsfactory` | Correct gdsfactory package is importable from gf7. |

If `KLAYOUT_PY_SITE` points to `.local`, fix the launcher before changing Python code.

## Command syntax

General command form:

```bash
skyspice2klayout_all_magicgr_analogproplus [options] <input.spice> <output.gds>
```

Common options:

| Option | Values | Meaning |
| --- | --- | --- |
| `--doctor` | none | Print environment and path checks without generating layout. |
| `--wizard` | none | Run the interactive terminal UI. |
| `--backend` | `gdsfactory`, `magic`, `hybrid` | Choose MOS generation strategy. |
| `--placement` | `analogpro`, `schematic` | Choose placement strategy. |
| `--ratlines` | `off`, `points`, `full`, `both` | Choose whether to generate separate ratline reference GDS files. |
| `--flat` | none | Use flat output behavior if supported by the launcher/script version. |

Frequently used environment variables:

| Variable | Typical value | Purpose |
| --- | --- | --- |
| `SKY_UNIT_ARRAY_MODE` | `plan` or `layout` | Plan unit arrays or physically replace large devices with unit-array wrappers. |
| `SKY_GUARD_RING` | `yes` or `no` | Enable selected guard-ring handling. |
| `SKY_GR_INSTS` | `1 2 3 4` or `all` | Select which MOS devices receive Magic guard-ring generation. |
| `SKY_MAGIC_ALL_GUARD_RINGS` | `1` | Force all supported MOS devices into Magic guard-ring mode. |
| `SKY_CC_MODE` | `off`, `plan`, `markers`, `layout_hint` | Common-centroid planning behavior. |
| `SKY_DUMMY_MODE` | `marker` or `off` | Dummy planning marker behavior. |
| `SKY_RATLINE_GDS` | `off`, `points`, `full`, `both` | Ratline reference GDS generation. |
| `SKY_MAGIC_DRC_CHECK` | `1` | Run Magic DRC smoke check helper. |
| `SKY_LVS_CHECK` | `1` | Run Magic extract/Netgen LVS helper if available. |

## Run folder behavior

Every normal run creates a separate output folder under the current working directory:

```text
./skyspice_runs/<output_name>_<timestamp>/
```

Example:

```text
./skyspice_runs/test_full_visual_20260621_204003/
```

This keeps the project folder clean. The command can be run repeatedly without overwriting older reports unless the exact output behavior is changed manually.

| File group | Examples | Purpose |
|---|---|---|
| Main layout | `<name>.gds` | Clean layout view that I open in KLayout. |
| Ratline views | `<name>_ratpoints.gds`, `<name>_ratlines_full.gds` | Optional static connectivity reference views. |
| Reports | `.report.json`, `.device_map.csv`, `.routing_priority.csv`, `.topology.json` | Debugging and analysis outputs. |
| Run metadata | `manifest.json`, `README_run.md`, `rerun.sh` | Reproducibility and run tracking. |
| Dashboard | `index.html` | Quick browser view of important run files. |

If I only want a clean main output without extra ratline files, I use `--ratlines off`.

## Run modes

### MOS backend modes

| Backend | What it does | When I use it |
| --- | --- | --- |
| `gdsfactory` | All MOS devices use the SKY130 Python/gdsfactory direct-draw path. | To test the Python draw helper flow and normal MOS generation. |
| `magic` | Selected guard-ring MOS use Magic. In v12, it should not blindly guard-ring everything unless all-guard mode is forced. | When I want Magic-generated guard-ring devices. |
| `hybrid` | Selected guard-ring MOS use Magic; remaining MOS use gdsfactory. | My usual mode. It balances Magic guard-ring generation with gdsfactory direct draw. |

### Placement modes

| Placement mode | Intuition | What it tries to do |
| --- | --- | --- |
| `analogpro` | More layout-aware and topology-aware. | Uses detected topology hints for mirrors, stacks, reference branches, large devices, and analog grouping. |
| `schematic` | Simpler and easier to compare with the input netlist. | Follows SPICE/netlist order more directly and keeps larger devices close together in one row. |

### Ratlines and ratpoints

Ratlines are debug/reference GDS geometry on non-fabrication layers. They are not real routed metal.

| Option | Output |
| --- | --- |
| `off` | Only the main clean GDS. |
| `points` | Main GDS plus terminal marker GDS. |
| `full` | Main GDS plus full connected ratline GDS. |
| `both` | Main GDS plus both point markers and full ratlines. |

Expected ratline files:

| File | Use |
| --- | --- |
| `<name>.gds` | Main clean layout. |
| `<name>_ratpoints.gds` | Terminal point markers only. Useful when full ratlines are too visually noisy. |
| `<name>_ratlines_full.gds` | Static non-fab connectivity lines. Closest thing to a simple KiCad-style ratsnest view. |

Limitations:

| Limitation | Meaning |
| --- | --- |
| Not routed metal | The ratline GDS is visual reference geometry only. |
| Not live | If I move devices manually, ratlines do not update automatically. |
| Not DRC/LVS geometry | These are non-fab debug/reference layers. |

### Unit arrays, common-centroid markers, and dummy markers

| Feature | Command/setting | What it means |
| --- | --- | --- |
| Unit-array plan | `SKY_UNIT_ARRAY_MODE=plan` | Writes unit-array reports but does not replace physical MOS layout. Best for initial verification. |
| Unit-array layout | `SKY_UNIT_ARRAY_MODE=layout` | Replaces large MOS devices with unit-array wrapper cells. Useful for layout exploration. |
| Unit width | `SKY_UNIT_W_UM=1` | Target unit width for splitting large MOS devices. |
| Minimum W for unit array | `SKY_UNIT_ARRAY_MIN_W_UM=8` | Only devices with W >= this value are considered for unit-array conversion. |
| Common-centroid markers | `SKY_CC_MODE=markers` | Adds non-fab matching/common-centroid planning markers. |
| Dummy markers | `SKY_DUMMY_MODE=marker` | Adds non-fab dummy-device planning markers. Does not create real dummy MOS devices. |

I usually test with `plan` first because extraction may see a physical unit array as many parallel MOS devices instead of one large MOS.

## Wizard mode

Run:

```bash
skyspice2klayout_all_magicgr_analogproplus \
  --wizard \
  examples/BGR_sky130_ckt.spice \
  test_wizard.gds
```

The wizard asks only the main choices. Everything else stays at the script defaults.

| Wizard prompt | Options | Meaning |
| --- | --- | --- |
| Run profile | `quick`, `pro`, `verify`, `clean` | Controls how much planning and verification helper logic is enabled. |
| MOS backend | `magic`, `gdsfactory`, `hybrid` | Chooses how MOS devices are generated. |
| Placement style | `analogpro`, `schematic` | Chooses topology-aware placement or netlist-order placement. |
| Physical unit arrays | `y`, `n` | Controls whether large MOS devices are physically replaced by unit-array wrappers. |
| Common-centroid mode | `off`, `plan`, `markers`, `layout_hint` | Controls common-centroid planning files and visible marker geometry. |
| Dummy mode | `marker`, `off` | Adds or disables dummy planning markers. |
| Magic DRC | `y`, `n` | Runs Magic DRC helper when enabled. |
| LVS helper | `y`, `n` | Attempts Magic extraction and Netgen comparison if setup supports it. |
| Ratline GDS | `off`, `points`, `full`, `both` | Generates optional ratline reference files. |
| Guard-ring selection | instance numbers, `all`, `n` | Selects which MOS devices get Magic guard-ring generation. |

Guard-ring prompt example:

```text
1) X1 ...
2) X2 ...
3) X3 ...
4) X4 ...

Add guard rings to selected FETs? [y/N or instance numbers]:
```

For the BGR test case, I can type:

```text
1 2 3 4
```

or type `y`, then enter `1 2 3 4` at the next prompt.

## Command examples

### 1. Basic working folder

```bash
cd ~/Analogprov12plus_runs
ls
```

Expected:

```text
README.md
analog_py_draw_klayout
examples
required_docs
required_scripts
skyspice_runs
```

### 2. gdsfactory backend test

This checks whether the gf7/gdsfactory path is working.

```bash
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend gdsfactory \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_gdsfactory_fixed.gds
```

Expected idea:

```text
Direct draw used: 18
Fixed GDS used  : 1
Missing         : 0
```

If this fails, it is usually a gf7/path/API issue, not a SPICE parsing issue.

### 3. Hybrid selected guard-ring test

This is my main useful mode.

```bash
SKY_UNIT_ARRAY_MODE=plan \
SKY_GUARD_RING=yes \
SKY_GR_INSTS="1 2 3 4" \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_hybrid_fixed.gds
```

Expected behavior:

```text
X1 X2 X3 X4  -> Magic guarded MOS
X5 onward    -> gdsfactory direct draw
XQ0          -> fixed GDS
Missing      -> 0
```

### 4. Magic selected-only test

In v12, `--backend magic` is selection-safe. It should not blindly guard-ring every MOS unless I explicitly ask for that.

```bash
SKY_UNIT_ARRAY_MODE=plan \
SKY_GUARD_RING=yes \
SKY_GR_INSTS="1 2 3 4" \
skyspice2klayout_all_magicgr_analogproplus \
  --backend magic \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_magic_selected_only.gds
```

Expected behavior:

```text
Only X1 X2 X3 X4 get Magic guard-ring generation.
Other MOS devices use the gf7/gdsfactory path.
```

If I intentionally want every MOS generated through Magic guard-ring mode:

```bash
SKY_MAGIC_ALL_GUARD_RINGS=1 \
SKY_GUARD_RING=yes \
SKY_GR_INSTS=all \
skyspice2klayout_all_magicgr_analogproplus \
  --backend magic \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_magic_all_guarded.gds
```

I do not use this as the default because it can make the layout bulky and guard-ring heavy.

### 5. Schematic placement test

```bash
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement schematic \
  --ratlines both \
  examples/BGR_sky130_ckt.spice \
  test_schematic_placement.gds
```

### 6. Ratline and ratpoint test

```bash
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines both \
  examples/BGR_sky130_ckt.spice \
  test_ratlines.gds
```

Open the generated files:

```bash
klayoutpdkfull skyspice_runs/*test_ratlines*/test_ratlines.gds
klayoutpdkfull skyspice_runs/*test_ratlines*/test_ratlines_ratpoints.gds
klayoutpdkfull skyspice_runs/*test_ratlines*/test_ratlines_ratlines_full.gds
```

### 7. Full visual test command

This turns on the main visual/planning features.

```bash
SKY_UNIT_ARRAY_MODE=layout \
SKY_UNIT_W_UM=1 \
SKY_UNIT_ARRAY_MIN_W_UM=8 \
SKY_GUARD_RING=yes \
SKY_GR_INSTS="1 2 3 4" \
SKY_CC_MODE=markers \
SKY_DUMMY_MODE=marker \
SKY_RATLINE_GDS=both \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines both \
  examples/BGR_sky130_ckt.spice \
  test_full_visual.gds
```

Expected outputs:

```text
skyspice_runs/test_full_visual_<timestamp>/
├── test_full_visual.gds
├── test_full_visual_ratpoints.gds
├── test_full_visual_ratlines_full.gds
├── test_full_visual.gds.report.json
├── test_full_visual.gds.device_map.csv
├── test_full_visual.gds.routing_priority.csv
├── test_full_visual.gds.topology.json
├── test_full_visual.gds.unit_arrays.csv
├── index.html
├── manifest.json
├── README_run.md
└── rerun.sh
```

### 8. Optional Magic DRC smoke check

```bash
SKY_UNIT_ARRAY_MODE=plan \
SKY_GUARD_RING=yes \
SKY_GR_INSTS="1 2 3 4" \
SKY_MAGIC_DRC_CHECK=1 \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_drc_check.gds
```

This writes a log like:

```text
test_drc_check.gds.magic_drc.log
```

I treat this as a smoke check, not final signoff.

### 9. Full verification helper command

This command keeps unit arrays in `plan` mode because physical unit-array replacement can confuse strict LVS unless device-summing is handled.

```bash
SKY_MOS_BACKEND=hybrid \
SKY_GUARD_RING=yes \
SKY_GR_INSTS="1 2 3 4" \
SKY_UNIT_ARRAY_MODE=plan \
SKY_CC_MODE=markers \
SKY_DUMMY_MODE=marker \
SKY_RATLINE_GDS=both \
SKY_MAGIC_DRC_CHECK=1 \
SKY_LVS_CHECK=1 \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines both \
  examples/BGR_sky130_ckt.spice \
  verify_full.gds
```

Expected useful files if the local setup supports the helper flow:

```text
verify_full.gds
verify_full.gds.report.json
verify_full.gds.magic_drc.log
verify_full.gds.magic_extract_lvs.log
verify_full.gds.netgen_lvs.log
```

I treat this as a helper check. It does not replace proper manual DRC/LVS review.

### 10. Open generated outputs

Open the main GDS:

```bash
klayoutpdkfull skyspice_runs/*test_full_visual*/test_full_visual.gds
```

Open the ratpoint view:

```bash
klayoutpdkfull skyspice_runs/*test_ratlines*/test_ratlines_ratpoints.gds
```

Open the full ratline view:

```bash
klayoutpdkfull skyspice_runs/*test_ratlines*/test_ratlines_ratlines_full.gds
```

Open the dashboard:

```bash
xdg-open skyspice_runs/*test_full_visual*/index.html
```

### 11. Wizard run

```bash
skyspice2klayout_all_magicgr_analogproplus \
  --wizard \
  examples/BGR_sky130_ckt.spice \
  test_wizard.gds
```


## Generated outputs

| Output | What I use it for |
| --- | --- |
| `<name>.gds` | Main clean generated layout. |
| `<name>_ratpoints.gds` | Terminal point reference GDS. |
| `<name>_ratlines_full.gds` | Static non-fab ratline reference GDS. |
| `<name>.gds.report.json` | Main run summary. |
| `<name>.gds.device_map.csv` | Device instance, model, method, nets, parameters, and raw SPICE info. |
| `<name>.gds.nets.json` | Net summary. |
| `<name>.gds.routing_priority.csv` | Important nets and routing hints. |
| `<name>.gds.topology.json` | Detected analog topology groups. |
| `<name>.gds.layout_warnings.csv` | Layout cautions and review notes. |
| `<name>.gds.unit_arrays.csv` | Unit-array planning/replacement summary. |
| `index.html` | Run dashboard. |
| `manifest.json` | Machine-readable run metadata. |
| `README_run.md` | Run-specific notes. |
| `rerun.sh` | Reproducible command for that run. |

Useful inspection commands:

```bash
xdg-open skyspice_runs/*test_full_visual*/index.html

jq '.placed_total, .missing_total, .placement_summary, .ratlines, .unit_arrays' \
  skyspice_runs/*test_full_visual*/test_full_visual.gds.report.json

column -s, -t < skyspice_runs/*test_full_visual*/test_full_visual.gds.device_map.csv | less -S

column -s, -t < skyspice_runs/*test_full_visual*/test_full_visual.gds.routing_priority.csv | less -S
```

## Troubleshooting

| Problem | Likely reason | What I check first |
| --- | --- | --- |
| `KLAYOUT_PY_SITE` shows `.local` | Launcher is not forcing gf7. | Patch the launcher. Do not change placement logic first. |
| `dev_temp already exists` | Helper cells created repeated internal names. | Use the gf7-compatible launcher/script and confirm the active launcher path. |
| `add_array`, `spacing`, or `move` errors | gdsfactory/KFactory API mismatch. | Confirm `~/klayout_gf7` and gdsfactory 7.x. |
| `.spice` opens as a layout stream | Launcher passed SPICE directly to KLayout. | Launcher should convert args to `SKY_NETLIST` and `SKY_OUT`. |
| `Unknown option: --` | KLayout version rejects extra `--`. | Use a launcher that avoids passing `--` to KLayout. |
| Magic works but hybrid/gdsfactory fails | Magic path is healthy, gdsfactory path is not. | Run `--doctor`, then test gdsfactory-only. |
| Ratlines stay static after moving devices | Ratlines are not live. | Regenerate ratlines after changing placement. |

Doctor check again:

```bash
skyspice2klayout_all_magicgr_analogproplus --doctor
```

Good path:

```text
KLAYOUT_PY_SITE : /home/<user>/klayout_gf7/lib/python3.12/site-packages
```

Bad path:

```text
KLAYOUT_PY_SITE : /home/<user>/.local/lib/python3.12/site-packages
```

## Validation order

Run these in order after a fresh setup or after moving the project to another machine:

```bash
# 1. Check paths
skyspice2klayout_all_magicgr_analogproplus --doctor

# 2. Test gdsfactory direct draw
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend gdsfactory \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_gdsfactory_fixed.gds

# 3. Test hybrid selected guard rings
SKY_UNIT_ARRAY_MODE=plan \
SKY_GUARD_RING=yes \
SKY_GR_INSTS="1 2 3 4" \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines off \
  examples/BGR_sky130_ckt.spice \
  test_hybrid_fixed.gds

# 4. Test schematic placement
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement schematic \
  --ratlines both \
  examples/BGR_sky130_ckt.spice \
  test_schematic_placement.gds

# 5. Test ratline files
SKY_UNIT_ARRAY_MODE=plan \
skyspice2klayout_all_magicgr_analogproplus \
  --backend hybrid \
  --placement analogpro \
  --ratlines both \
  examples/BGR_sky130_ckt.spice \
  test_ratlines.gds

# 6. Test wizard UI
skyspice2klayout_all_magicgr_analogproplus \
  --wizard \
  examples/BGR_sky130_ckt.spice \
  test_wizard.gds
```

Once these pass, I consider the local setup usable.

## Final note

This is not meant to replace real analog layout skill. I made it because I wanted a cleaner, more useful SPICE-to-KLayout starting point than the rough native import experience I was fighting with.

It is a playground project, but it became useful enough that I wanted the setup to be reproducible. If something breaks, I first check the launcher paths, then the gf7 environment, then the PDK helper paths. I do not randomly change the working Python flow unless the path and environment checks are clean.
