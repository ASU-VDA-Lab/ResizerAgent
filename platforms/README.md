# `platforms/`

Per-PDK files sourced by the executor's TCL.

| File | What it is |
|------|------------|
| `asap7/preamble.tcl` | Loads ASAP7 LEFs (tech + per-V_t standard cells) and Liberty files. Sourced via `$env(PDK_PREAMBLE)`. |
| `asap7/cell_catalog.xml` | List of available ASAP7 cell masters; consumed by the planner agent's cell-naming reference. |
| `nangate45/preamble.tcl` | NanGate45 LEF + Liberty loader. Same role as the ASAP7 preamble. |
| `nangate45/cell_catalog.xml` | NanGate45 cell-master catalogue. |
