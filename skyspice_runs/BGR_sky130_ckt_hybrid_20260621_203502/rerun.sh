#!/usr/bin/env bash
set -euo pipefail

# Re-run from the original command directory or edit paths below.
export SKY_GUARD_RING=yes
export SKY_GR_INSTS='1 2 3 4'
export SKY_UNIT_ARRAY_MODE=plan

skyspice2klayout_all_magicgr_analogproplus /home/kafkayash/Analogproplus_runs/BGR_sky130_ckt.spice BGR_sky130_ckt_hybrid.gds
