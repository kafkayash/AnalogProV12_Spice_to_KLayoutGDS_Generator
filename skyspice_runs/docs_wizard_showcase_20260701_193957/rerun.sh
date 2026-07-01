#!/usr/bin/env bash
set -euo pipefail

# Re-run from the original command directory or edit paths below.
export SKY_RATLINE_GDS=both
export SKY_PLACEMENT_STYLE=schematic

skyspice2klayout_all_magicgr_analogproplus /home/kafkayash/Analogprov12plus_runs/examples/BGR_sky130_ckt.spice docs_wizard_showcase.gds
