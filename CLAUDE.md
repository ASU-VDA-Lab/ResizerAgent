# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A multi-agent CLI-driven OpenROAD timing-closure framework. Four agents
(Reporter → Planner → Executor → Selector) iterate over a post-CTS design to
minimize WNS / TNS. Every iteration runs `repair_timing` **plus global route**
inside the loop; the report/decision signal is after-GR slack.

**Top directory:** `/home/atmadipd/new_openroad/context_scripts/cli_based_flow/2A-new-context`
**Single entry point:** `run.py` at the top directory.

**Variant:** Baseline — sandbox planner + sandbox selector, fresh Selector session every iteration (no `--resume`). Use for comparison against `2A-cont-context`.

---

## Running the Flow

All commands run from the top directory. `run.py` is stage-based: pick a
single stage with `--run-stage`, or clean a stage with `--clean`.

```bash
# Stage 0 — CTS inside Docker (also copies 4_1_cts.odb → base/base_cts.odb)
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage base

# Stage 1 — Default repair_timing + GR (produces default.odb after-GR)
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage default

# Stage 2 — LLM iterations (Reporter/Planner/Executor/Selector loop)
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage LLM-iterations --max-iterations 6

# Stage 3 — Backend flows (each runs post-GR shim + detail_route + finish)
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage backend_default
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage backend_best
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage backend_rank2     # also rank3, rank4

# Full pipeline in one call
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage all --max-iterations 6
```

### CLI arguments (authoritative list — see `run.py:2592`)

| Flag | Required | Notes |
|---|---|---|
| `--design` | yes | Design name as used in `designs/<pdk>/<design>/config.mk` |
| `--agent` | yes | Agent label (e.g. `claude`) — namespaces work_dir output |
| `--pdk` | yes | `asap7` or `nangate45` |
| `--run-stage` | one of | `base`, `default`, `LLM-iterations`, `backend_default`, `backend_best`, `backend_rank2`, `backend_rank3`, `backend_rank4`, `all` |
| `--clean` | one of | `base`, `default`, `agentic_flow`, `all` |
| `--max-iterations` | no | LLM iterations upper bound (default 15) |
| `--start-iteration` | no | Resume LLM loop from this iteration (default 1) |
| `--claude-bin` | no | Path to Claude CLI (default: `claude`) |

Exactly one of `--clean` or `--run-stage` must be provided.

---

## Stage 0 prerequisite — ORFS CTS inside Docker

`run.py --run-stage base` runs `make cts-a` for you inside Docker; no manual
`docker run -it` needed. If you must run by hand:

```bash
docker run --rm -it --user root \
  -e FLOW_HOME=/workspace/OpenROAD-flow-scripts/flow \
  -v /home/atmadipd/new_openroad/context_scripts/cli_based_flow/4-agent-flow:/workspace \
  -w /workspace/OpenROAD-flow-scripts \
  openroad/flow-ubuntu22.04-builder:0b569c /bin/bash

# Inside Docker, in /workspace/OpenROAD-flow-scripts/flow:
make clean_all DESIGN_CONFIG=./designs/<pdk>/<design>/config.mk
make cts-a SKIP_CTS_REPAIR_TIMING=1 DESIGN_CONFIG=./designs/<pdk>/<design>/config.mk \
  OPENROAD_EXE=/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad \
  YOSYS_EXE=/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys \
  ENABLE_POST_CTS_CLI_REPAIR_TIMING=0
```

Docker paths: OpenROAD at `/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad`,
Yosys at `/usr/local/bin/yosys`, mounted workspace at `/workspace`.

---

## Architecture

### Two-Agent Loop (Reporter and Executor are pure Python)

```
run.py (orchestrator)
  └─ Per iteration:
       1. Reporter  → run_reporter() in run.py — pure Python, no LLM call.
                      Reads base_cts/default/prev-best artifacts, parses
                      metrics/paths/neighbors/cell-catalog/GR-metrics,
                      writes reporter_baseline_prompt.txt.
       2. Planner   → Claude CLI (model: Opus) reads reporter prompt,
                      writes planner_decision.json (sequence/staged/eco)
       3. Executor  → Scripts/python/executor.py — pure Python, no LLM call.
                      Validates planner_decision.json, generates run_plan.tcl
                      for every plan under planX/. Workers launched by
                      launch_workers.py in parallel inside Docker.
       4. Selector  → Claude CLI (model: Opus) promotes best plan to
                      Iteration<N>/best/ and writes selector_decision.json
                      (continue | stop | backtrack)
  └─ Best plan's after-GR output.odb promoted as seed for next iteration
```

### Per-plan run — what runs inside `run_plan.tcl`

```
read_db/read_sdc → estimate_parasitics -placement (pre-RT)
  → repair_timing (moves chosen by Planner; or staged; or ECO)
  → detailed_placement + check_placement
  → pin_access + global_route + set_propagated_clock
    + estimate_parasitics -global_routing    ← Blocks A–D of ORFS global_route.tcl
  → write_db/write_sdc (after-GR state)
  → source generate_design_artifacts.tcl (timing/area/power at GR parasitics)
  → source generate_placement_report.tcl
```

### Key source files

| File | Role |
|------|------|
| `run.py` | Single orchestrator entry — all stages, CLI parse, dispatch |
| `Scripts/python/prepare_design.py` | Base-stage prep: 4_1_cts → base_cts.odb, run_default_rt, artifact generation |
| `Scripts/python/flow_controller.py` | Stage helper utilities consumed by run.py |
| `Scripts/python/launch_workers.py` | Parallel Docker launcher for plan workers (per-plan `run_plan.tcl`) |
| `Scripts/python/generate_cts.py` | Wrapper for `make cts-a` invocation |
| `Scripts/tcl/run_default_rt.tcl` | Default `repair_timing` + legalization + **GR (Blocks A–D)** |
| `Scripts/tcl/generate_design_artifacts.tcl` | Extracts timing/area/power/violating-endpoint reports at caller's parasitic state |
| `Scripts/tcl/generate_placement_report.tcl` | Spatial utilization / free-slot / critical-path cell locations |
| `Scripts/tcl/write_sdc_from_odb.tcl` | Dumps SDC back out of an ODB when needed |
| `Scripts/schemas/planner_decision.schema.json` | Planner output contract |
| `agents/planner/AGENTS.md` | Planner prompt (reads reporter output, emits sequence/staged/eco plans) |
| `Scripts/python/executor.py` | Pure-Python Executor — validates planner JSON, generates run_plan.tcl for sequence/staged/eco plans |
| `agents/selector/AGENTS.md` + `PARAMETERS.md` | Selector prompt + plateau/backtrack knobs |
| `agents/openroad_reference.md` | Shared: repair_timing knobs, 9 moves, Docker constants |
| `agents/nangate45.md` / `agents/asap7.md` | PDK cell-naming, drive strength, VT tiers, unsizable cells |
| `Platforms/<pdk>/preamble.tcl` + `cell_catalog.*` | PDK LEF/LIB loader (not read by agents — TCL/XML only) |

### Design Stage Semantics

- **`base_cts`** — raw post-CTS snapshot (`base/base_cts.odb`), no `repair_timing`, no GR. Placement parasitics only. This is the seed input for both `default` and the LLM loop.
- **`default`** — ORFS default `repair_timing` + legalization + **global route** (after-GR parasitics). Files under `default/`.
- **`plan<i>`** (LLM plans) — Planner-chosen `repair_timing` moves + legalization + **global route** (after-GR parasitics). Files under `work_dir/<agent>/<design>/LLM_iterations/Iteration<N>/plan<i>/`.

### Loop-vs-backend split (important)

Every `default` and `plan<i>` run executes **Blocks A–D** of ORFS `global_route.tcl`:
`pin_access` → `global_route` → `set_placement_padding` + `set_propagated_clock` → `estimate_parasitics -global_routing`.

Reported WNS/TNS/area/power is measured **after those GR blocks** — not at placement parasitics.

Once the Selector promotes the best plan, the backend flow copies the after-GR ODB+SDC
as `4_1_cts.odb/sdc` into the ORFS results directory and runs `make route finish`
(ORFS native). ORFS's own `5_1_grt` stage handles post-GR repair + detail route + finish.

### Outputs

For design `D` under agent `A`:

- `work_dir/A/D/base/` — `base_cts.*`, base reports
- `work_dir/A/D/default/` — default after-GR ODB/SDC + reports
- `work_dir/A/D/LLM_iterations/Iteration<N>/` — per-iteration planner/selector JSON + reporter prompt + `plan<i>/` subdirs + `best/`
- `work_dir/A/D/LLM_iterations/Best_solutions/rankings.json` — ranked plans across iterations (consumed by backend_rank stages)
- `work_dir/A/D/{design}_baseline.csv` — FP/GP/DP/CTS/default_repair/GRT/DR row-per-stage ledger
- `work_dir/A/D/{design}_agentic[_r<N>].csv` — same schema for LLM-best / rank-N
- `work_dir/A/D/experiment_summary.csv` — per-iteration summary across all plans
- `work_dir/A/D/runtimes.csv` — wall-time per stage/iteration/plan
- `work_dir/A/D/tokens.csv` — token usage + cost per LLM call (input/output/cache tokens, model, role)
- `run_logs/<design>_<pdk>/` — Docker/OpenROAD/make stdout+stderr per step

---

## Ground rules for modifications

1. **Post-CTS design intent** — this is not a post-global-placement flow. Stage 0 goes through CTS; the LLM loop starts from post-CTS.
2. **GR-in-loop invariant** — every `default` and `plan<i>` run ends with Blocks A–D of GR and reports after-GR metrics. Do not remove the GR block from `run_default_rt.tcl` or from the executor's common postamble. Do not add `repair_timing` after `global_route` inside a plan — that belongs in the backend shim.
3. **Explicit stage separation** — `base_cts`, `default`, and `plan<i>` are distinct states. Do not conflate them or reuse one's filename for another.
4. **ORFS canonical handoffs** — the backend shim writes `5_1_grt.odb` / `5_1_grt.sdc` / `route.guide` in ORFS's results layout; `make detail_route finish` consumes those. Do not swap in ad-hoc filenames.
5. **Editing priority order** — stage correctness → base/default artifacts → loop GR postamble → backend shim → agent prompts.
6. **Minimal TCL** — standalone TCL drivers should do the minimum required work; all scratch/log output goes through `OUTPUT_DIR`.
7. **Two ORFS trees in Docker** — the mounted `/workspace/OpenROAD-flow-scripts` and any image-baked ORFS tree may both exist. Be explicit about which one is being used for any given call.

---

## Dependencies

- **Python 3** (stdlib only — no pip packages required)
- **OpenROAD** and **Yosys** inside Docker image `openroad/flow-ubuntu22.04-builder:0b569c`
- **Claude CLI** binary (`claude`), configured with model access to `claude-opus-4-7` (Planner/Selector) and `claude-sonnet-4-6` (Executor + retry)
- **ASAP7 PDK** at `OpenROAD-flow-scripts/flow/platforms/asap7/`
- **Nangate45 PDK** at `OpenROAD-flow-scripts/flow/platforms/nangate45/`

---

## Logging

Per-stage stdout goes under `run_logs/<design>_<pdk>/`. The orchestrator also
appends timing rows to `work_dir/<agent>/<design>/runtimes.csv` and per-call
token usage to `work_dir/<agent>/<design>/tokens.csv`. Report only command
/stage name, success/failure, and key metrics in conversation — do not paste
raw tool output.
