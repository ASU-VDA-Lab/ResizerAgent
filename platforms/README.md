# `Platforms/`

PDK-specific artifacts consumed by `run.py` and the OpenROAD TCL drivers.
Each PDK lives in its own subdirectory with the same three-file layout:

| File | Role |
|------|------|
| `preamble.tcl` | Loaded at the top of every OpenROAD session. Reads the PDK's LEFs / LIBs / RC corner so the driver script can immediately `read_db` + `read_sdc` and start work. |
| `cell_catalog.xml` | Enumeration of every cell master in the PDK, grouped by family / size / VT tier. Produced once by `Scripts/python/utils/build_cell_catalog.py` and consumed by the Reporter at runtime to show the Planner available sizeup targets and equivalent-VT swaps. |
| `reference.md` | Human-readable cell-naming convention and design rules for the PDK. Not loaded at runtime — kept here for developer reference. |

The PDK selected by `--pdk` on the `run.py` CLI is wired through
`Scripts/python/pdk_configs/<pdk>.py`, which references this directory via
the workspace root.

## Supported PDKs

| Directory | PDK | Multi-VT | Cell-name convention | Notes |
|-----------|-----|----------|----------------------|-------|
| `asap7/` | ASAP7 (7 nm predictive) | Yes (RVT / LVT / SLVT) | `<FAMILY><drive>_ASAP7_<VT>` | Has unsizable adder cells (HA, FA). |
| `nangate45/` | Nangate45 / FreePDK45 | No | `<FAMILY>_X<size>` | Single VT — no swap moves possible. |

## Adding a new PDK

1. Make sure the PDK is available under your ORFS tree at
   `OpenROAD-flow-scripts/flow/platforms/<pdk>/`.
2. Create a sibling directory here: `Platforms/<pdk>/`.
3. Write `preamble.tcl` for the new PDK (use the existing ones as templates).
4. Build the cell catalog:
   ```bash
   python3 Scripts/python/utils/build_cell_catalog.py \
       --lib-dir OpenROAD-flow-scripts/flow/platforms/<pdk>/lib/ \
       --pdk <pdk> \
       --output Platforms/<pdk>/cell_catalog.xml
   ```
5. Add a `Scripts/python/pdk_configs/<pdk>.py` config (copy `asap7.py` or
   `nangate45.py` as a starting point) and register it in
   `Scripts/python/pdk_configs/__init__.py`.
6. Pass `--pdk <pdk>` on the `run.py` CLI.
