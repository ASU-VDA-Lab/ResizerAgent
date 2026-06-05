# `scripts/`

## `python/`


| File | What it is |
|------|------------|
| `executor.py` | Validates `planner_decision.json` and emits one `run_plan.tcl` per plan under `planX/`. |
| `launch_workers.py` | Runs one Docker / OpenROAD process per `planX/`, captures logs, and classifies errors. |
| `generate_cts.py` | Invokes `make clean_all && make cts` inside ORFS  |
| `parse_gr_metrics.py` | Parses `global_route` logs into a structured summary. |
| `generate_report.py` | Compiled text report for a run (floorplan / global placement / detail placement / clock tree synthesis / global route / detail route  + iteration trajectory). |

### `python/utils/`

| File | What it is |
|------|------------|
| `build_cell_catalog.py` | Run once when adding a new PDK; parses Liberty files into `platforms/<pdk>/cell_catalog.xml`. |
| `extract_max_paths.py` | Parses an OpenROAD timing report into a per-pin path enumeration. |

### `python/pdk_configs/`

| File | What it is |
|------|------------|
| `asap7.py` | ASAP7 PDK bundle (multi-V_t; ASAP7-specific unsizable cells). |
| `nangate45.py` | NanGate45 PDK bundle (single-V_t). |

## `tcl/`

| File | What it is |
|------|------------|
| `run_default.tcl` | Default-stage driver: load PDK + `base_cts.odb` → `repair_timing` → detailed placement → writes DB → global route → post-GR metrics. |
| `generate_base_artifacts.tcl` | Base-stage timing/area/power / worst-paths / metrics from `base_cts.odb` at placement parasitics. |
| `generate_design_artifacts.tcl` | Reusable report generator for an arbitrary ODB; configured via env vars. |
| `generate_placement_report.tcl` | Spatial placement report. |


## `schemas/`

| File | What it is |
|------|------------|
| `planner_decision.schema.json` | Output contract for the planner agent. |
| `selector_decision.schema.json` | Output contract for the selector agent's LLM portion. |
