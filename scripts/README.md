# `scripts/`

Non-orchestrator code consumed by `run.py`.

| Subdir | What it holds |
|--------|---------------|
| `python/` | Python modules: report/execute/select pre-compute, worker launcher, plotting helpers. |
| `tcl/` | OpenROAD TCL drivers invoked inside Docker. |
| `schemas/` | JSON Schemas the planner and selector outputs validate against. |

## `python/`

### Run-loop modules

| File | What it is |
|------|------------|
| `executor.py` | Validates `planner_decision.json` and emits one `run_plan.tcl` per plan under `planX/`. Does not invoke Docker. |
| `selector_prep.py` | Pre-computes mechanical fields (rankings, regression flags, plateau diagnostics) before the selector LLM call. |
| `launch_workers.py` | Runs one Docker / OpenROAD process per `planX/`, captures logs, classifies errors. |
| `generate_cts.py` | Invokes `make clean_all && make cts-a` so `4_1_cts.odb` / `.sdc` land in the ORFS results tree. |
| `parse_gr_metrics.py` | Parses `[INFO GRT-*]` lines from `global_route` logs into a structured summary. |
| `extract_stuck_paths.py` | Walks iterations and tallies follow-through of selector stuck-path → next planner. |

### Analysis / dataset

| File | What it is |
|------|------------|
| `generate_report.py` | Compiled text report for a run (FP / GP / DP / CTS / repair / GRT / DR ledger + iteration trajectory). |
| `build_dialogue_dataset.py` | Walks all experiments and writes a CSV summarising every planner → selector dialogue. |

### Plotting

| File | What it is |
|------|------------|
| `plot_calibration.py` | Planner-predicted ΔWNS vs actual ΔWNS. |
| `plot_decision_trajectory.py` | Per-experiment WNS-over-iterations trajectory with selector-decision markers. |
| `plot_pareto.py` | Effective CP vs DR power, one curve per rank, across clock periods. |
| `plot_pareto_grid.py` | 2-row × N-column Pareto grid; rows = repair vs DR stage. |
| `plot_pareto_grid_nangate45.py` | NanGate45 wrapper around `plot_pareto_grid.py`. |
| `plot_util_sweep.py` | Effective-CP vs target-util curve per rank. |
| `plot_util_grid.py` | 2-row util-sweep grid (left-y = power, right-y = util). |
| `plot_util_3d.py` | 3-D util / CP / power scatter. |

### `python/utils/`

| File | What it is |
|------|------------|
| `build_cell_catalog.py` | Run once when adding a new PDK; parses Liberty files into `platforms/<pdk>/cell_catalog.xml`. |
| `extract_max_paths.py` | Parses an OpenROAD timing report into a per-pin path enumeration. |

### `python/pdk_configs/`

| File | What it is |
|------|------------|
| `__init__.py` | `get(name)` returns the `_PDK` dataclass for a PDK. |
| `asap7.py` | ASAP7 PDK bundle (multi-V_t; ASAP7-specific unsizable cells). |
| `nangate45.py` | NanGate45 PDK bundle (single-V_t). |

## `tcl/`

| File | What it is |
|------|------------|
| `run_default.tcl` | Default-stage driver: load PDK + `base_cts.odb` → `repair_timing` → DPL → write pre-GR DB → GR → post-GR metrics. |
| `generate_base_artifacts.tcl` | Base-stage timing / area / power / worst-paths / metrics from `base_cts.odb` at placement parasitics. |
| `generate_design_artifacts.tcl` | Reusable report generator for an arbitrary ODB; configured via env vars. |
| `generate_placement_report.tcl` | Spatial placement report (density grid, critical-path cell locations). |
| `eco_buf_insert.tcl` | Defines `eco_insert_buffer` — ODB-level buffer-insertion procedure used by targeted-fix plans. |

## `schemas/`

| File | What it is |
|------|------------|
| `planner_decision.schema.json` | Output contract for the planner agent. Validates 1–7 candidate plans across the three plan types. |
| `selector_decision.schema.json` | Output contract for the selector agent's LLM portion. `selector_prep.py` merges the mechanical fields after the LLM writes this file. |
