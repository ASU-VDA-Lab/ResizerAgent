# `Scripts/`

All non-orchestrator code consumed by `run.py`. Three subtrees:

| Subdir | Contents |
|--------|----------|
| `python/` | Python implementation of Reporter, Executor, Selector pre-compute, launch_workers, plotting / dataset helpers. |
| `tcl/` | Stand-alone OpenROAD TCL drivers invoked inside Docker per stage / plan. |
| `schemas/` | JSON Schemas that the Planner and Selector outputs must validate against. |

---

## `python/`

### Core run-loop modules

| File | Description |
|------|-------------|
| `executor.py` | Pure-Python **Executor agent**. Validates `planner_decision.json` and emits one `run_plan.tcl` per plan under `planX/`. Does NOT invoke Docker or OpenROAD — that's `launch_workers.py`. Public entry points: `build_plans(iter_dir)`, `fix_tcl_errors(failures, iter_dir)`. |
| `selector_prep.py` | Pre-computes the mechanical parts of the Selector's job (plan rankings, regression flags, convergence diagnostics, iteration tables) outside the Claude sandbox so the LLM only handles semantic reasoning. Exposes `build_context()` and `merge_selector_output()`. |
| `launch_workers.py` | Parallel plan executor: runs one Docker / OpenROAD process per `planX/` directory, captures per-plan logs, classifies errors, writes structured metrics. Selection is NOT its job — the Selector agent runs after. |
| `generate_cts.py` | Stage 0 helper: invokes `make clean_all && make cts-a` for a given design inside Docker so `4_1_cts.odb` / `4_1_cts.sdc` land in `OpenROAD-flow-scripts/flow/results/<pdk>/<design>/base/`. |
| `parse_gr_metrics.py` | Parses `[INFO GRT-*]` lines from `global_route` log output into a compact structured summary for the Reporter's iteration-N prompt. Exports `parse_gr_metrics(path)` and `format_gr_summary(metrics)`; also runnable as a CLI for ad-hoc inspection. |
| `extract_stuck_paths.py` | Phase-1B research artifact: walks iterations and records cases where iteration N+1's Planner targeted the endpoint the Selector flagged in iteration N's `stuck_path`. Output: a Markdown follow-through table sorted by actual ΔWNS. |

### Reporting / dataset / analysis

| File | Description |
|------|-------------|
| `generate_report.py` | Emits a compiled text report for a 4-agent run (FP / GP / DP / CTS / default_repair / GRT / DR ledger plus iteration trajectory). Supports standard `work_dir` layout or an explicit `--wdir` path for `clock_sweep` / custom layouts. |
| `build_dialogue_dataset.py` | Phase-0 survey: walks `util_sweeps/asap7` and `clock_sweep/orfs_fix/asap7` and writes a single CSV summarising every (experiment, iteration, plan) Planner→Selector dialogue. Feeds the calibration / stuck-path / trajectory artifacts. |

### Plotting (research figures, optional)

| File | Description |
|------|-------------|
| `plot_calibration.py` | Phase-1A figure: Planner-predicted ΔWNS (midpoint of `expected_outcome`) vs actual ΔWNS, coloured by over / under / accurate / regressed tag. Diagonal `y = x` reference. |
| `plot_decision_trajectory.py` | Phase-2B figure: per-experiment WNS-over-iterations trajectory, marker = Selector decision, colour = plateau diagnosis, arrows = backtrack targets. Reads chosen-plan rows from `dialogue_dataset.csv`. |
| `plot_pareto.py` | Per-design Pareto curve of effective clock period (CP − WNS) vs DR power, one curve per rank (baseline, rank1/best, rank2/3/4) across clock periods. Source: `clock_sweep/orfs_fix/asap7/`. |
| `plot_pareto_grid.py` | 2-row × N-column Pareto grid; top row = repair stage, bottom row = DR. Each panel = baseline vs `--vs-rank` (default rank 1) for one design. Reuses loaders from `plot_pareto.py`. |
| `plot_pareto_grid_nangate45.py` | Thin wrapper around `plot_pareto_grid.py` that retargets `CLOCK_SWEEP` to nangate45 and defaults `--designs` to the four bare names. |
| `plot_util_sweep.py` | Per-design effective-CP vs target-util curve, one per rank, across `<util>_<cp>` variants under `util_sweeps/asap7/`. |
| `plot_util_grid.py` | 2-row × N-column util-sweep grid: twin-y scatter with left-y = power (mW), right-y = target util (%). Top row = repair, bottom row = DR. |
| `plot_util_3d.py` | 3-D variant of the util-sweep grid: x = util, y = effective CP, z = power, with vertical stems and a dashed shadow on the floor plane. |

### `python/utils/`

| File | Description |
|------|-------------|
| `build_cell_catalog.py` | Run **once** when adding a new PDK. Parses the PDK's Liberty (`.lib`) files and writes `Platforms/<pdk>/cell_catalog.xml`, which the Reporter loads at runtime to show the Planner available cell sizes and sizeup targets. |
| `extract_max_paths.py` | Parses an OpenROAD timing report into an enumerated list of violating paths with 7 fields per pin: `inst/pin master cell_dly_ps net_dly_ps slew_ps cap_fF net`. Used by the Reporter when building per-path context for the Planner. |

### `python/pdk_configs/`

| File | Description |
|------|-------------|
| `__init__.py` | Registry helper: `get(name)` returns the `_PDK` dataclass for a PDK by name. Each PDK config exposes LEF / LIB filenames, lib subdir, setRC.tcl, cell-name regex (family / size / vt groups), VT tiers (fast → slow), and unsizable cell families. |
| `asap7.py` | ASAP7 PDK bundle — multi-VT, `NLDM/` lib subdir, lists ASAP7-specific unsizable cells (HA, FA, etc.). |
| `nangate45.py` | Nangate45 (FreePDK45 / Si2 NOCL) PDK bundle — single-VT, no lib subdir, `FAMILY_Xsize` cell naming. |

---

## `tcl/`

| File | Description |
|------|-------------|
| `run_default.tcl` | Default-stage driver: loads PDK + base_cts.odb + SDC → `estimate_parasitics -placement` → `repair_timing` (default or env `SEQUENCE`) → `detailed_placement` + `check_placement` → save pre-GR handoff DB → global route (Blocks A–D from ORFS `global_route.tcl`) → full after-GR metrics. Single-file entry-point for `--run-stage default`. |
| `generate_base_artifacts.tcl` | Base-stage combined artifact generator. One Docker invocation, one OpenROAD session: produces timing / area / power / worst-paths / metrics plus the placement report from raw post-CTS `base_cts.odb` at placement parasitics. Self-contained — no `source` of other generate scripts. |
| `generate_design_artifacts.tcl` | Generic timing / area / power / paths report generator from a given ODB. Configured via env vars (`INPUT_DB`, `SDC_FILE`, `OUTPUT_DIR`, `LEF_DIR`, `LIB_DIR`, `SETRC_TCL`, optional `STAGE_TAG`). Reused by base / default / per-plan stages. |
| `generate_placement_report.tcl` | Spatial placement report: die dimensions, utilization grid, critical-path cell locations with local density, worst-path bounding box, free row-slot count. Env-driven; consumed by the Planner prompt. |
| `eco_buf_insert.tcl` | Defines `eco_insert_buffer`: ECO buffer-insertion procedure that splits a net, moves selected sinks to a new buffer output via the ODB API (`make_net` / `make_instance` / `connect_pin`). Works around the OpenROAD `insert_buffer` crash after `replace_cell` (bug EST-0104). Sourced by `executor.py` when emitting `eco`-type plans. |

---

## `schemas/`

| File | Description |
|------|-------------|
| `planner_decision.schema.json` | Output contract for the Planner agent. Validates a decision committing to 1–7 execution plans (mix of `sequence`, `staged`, and `eco` types). Analysis is internal — only the decision, rationale, and plans are written. |
| `selector_decision.schema.json` | Output contract for the **LLM portion** of the Selector decision. Python pre-compute (`selector_prep.py`) merges all mechanical fields (`chosen_plan`, `chosen_metrics`, `regressed`, `convergence`, `iteration_table`, `plan_results`, `strategy_note.prediction_analysis`) AFTER the LLM writes this file. The final `selector_decision.json` on disk is a superset of this schema. |
