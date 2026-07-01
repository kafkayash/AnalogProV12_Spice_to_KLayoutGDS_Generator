#!/usr/bin/env bash
set -euo pipefail

# Re-run from the original command directory or edit paths below.
export SKY_UNIT_ARRAY_MODE=plan
export SKY_RATLINE_GDS=off
export SKY_PLACEMENT_STYLE=analogpro

skyspice2klayout_all_magicgr_analogproplus /home/kafkayash/Analogprov12plus_runs/examples/BGR_sky130_ckt.spice test_gdsfactory_fixed.gds
