# `scripts/`


| Subdir | What it holds |
|--------|---------------|
| `python/` | Python modules: report/execute/select pre-compute, worker launcher. |
| `tcl/` | OpenROAD TCL drivers invoked inside ORFS. |
| `schemas/` | JSON Schemas for the Planner and Selector Agents. |

## `python/`

### Run-loop modules

| File | What it is |
|------|------------|
| `executor.py` | Validates `planner_decision.json` and emits one `run_plan.tcl` per plan under `planX/`. |
| `launch_workers.py` | Runs one Docker / OpenROAD process per `planX/`, captures logs, and classifies errors. |
| `generate_cts.py` | Invokes `make clean_all && make cts` inside ORFS  |
| `parse_gr_metrics.py` | Parses `global_route` logs into a structured summary. |
| `generate_report.py` | Compiled text report for a run (FP / GP / DP / CTS / repair / GRT / DR ledger + iteration trajectory). |

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
| `run_default.tcl` | Default-stage driver: load PDK + `base_cts.odb` → `repair_timing` → Detailed Placement → writes DB → Global Route → post-GR metrics. |
| `generate_base_artifacts.tcl` | Base-stage timing/area/power / worst-paths / metrics from `base_cts.odb` at placement parasitics. |
| `generate_design_artifacts.tcl` | Reusable report generator for an arbitrary ODB; configured via env vars. |
| `generate_placement_report.tcl` | Spatial placement report. |


## `schemas/`

| File | What it is |
|------|------------|
| `planner_decision.schema.json` | Output contract for the planner agent. Validates 1–7 candidate plans across the three plan types. |
| `selector_decision.schema.json` | Output contract for the selector agent's LLM portion. `selector_prep.py` merges the mechanical fields after the LLM writes this file. |
