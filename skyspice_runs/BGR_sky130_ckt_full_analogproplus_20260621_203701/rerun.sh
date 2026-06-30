#!/usr/bin/env bash
set -euo pipefail

# Re-run from the original command directory or edit paths below.
export SKY_GUARD_RING=yes
export SKY_GR_INSTS='1 2 3 4'
export SKY_UNIT_ARRAY_MODE=layout
export SKY_UNIT_W_UM=1
export SKY_UNIT_ARRAY_MIN_W_UM=8
export SKY_CC_MODE=markers
export SKY_DUMMY_MODE=marker
export SKY_ANALOG_TEMPLATE=auto
export SKY_MAGIC_DRC_CHECK=1

skyspice2klayout_all_magicgr_analogproplus /home/kafkayash/Analogproplus_runs/BGR_sky130_ckt.spice BGR_sky130_ckt_full_analogproplus.gds
