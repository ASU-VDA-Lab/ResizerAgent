# ResizerAgent

A CLI-driven OpenROAD timing-closure framework. Each iteration moves through
four phases — **Report → Plan → Execute → Select** — to minimize Worst
Negative Slack (WNS) and Total Negative Slack (TNS) on a post-CTS design.
Every iteration runs `repair_timing` plus global route inside the loop, and
the reported metrics are after-GR slack. Three of the four phases delegate
their reasoning step to a Claude-CLI agent; the Report phase is fully
deterministic Python.

## The Four-Phase Loop

```
run.py (orchestrator)
  └─ Per iteration:
       Report   — pure Python. Parses base_cts / default / prev-best
                  artifacts (metrics, paths, neighbors, cell catalog, GR
                  metrics) and assembles reporter_baseline_prompt.txt for
                  the Plan phase. No agent.

       Plan     — Planner agent (Claude CLI, Opus,
                  agents/planner/AGENTS.md). Reads the reporter prompt and
                  writes planner_decision.json with 1–7 execution plans
                  (sequence / staged / eco) plus rationale and expected
                  outcomes.

       Execute  — hybrid. First pass is pure Python
                  (Scripts/python/executor.py): validates the planner JSON
                  and emits one run_plan.tcl per plan; launch_workers.py
                  then runs the plans in parallel inside Docker. If a
                  worker hits a TCL error, the Executor agent (Claude CLI,
                  Sonnet, agents/executor/AGENTS.md) repairs the TCL and
                  the worker re-runs.

       Select   — hybrid. Pure-Python selector_prep.py pre-computes the
                  mechanical fields (plan rankings, regression flags,
                  plateau / convergence diagnostics). The Selector agent
                  (Claude CLI, Opus, agents/selector/AGENTS.md) then rates
                  plans, names the worst stuck path with cell-level
                  guidance for the next Plan phase, and decides
                  continue / stop / backtrack. The promoted plan's after-GR
                  ODB becomes the seed for the next iteration.
```

## Repository layout

| Path | Contents |
|------|----------|
| `run.py` | Single entry-point orchestrator (all stages, CLI parse, dispatch). |
| [`Scripts/`](Scripts/) | Python + TCL + JSON schemas — see [`Scripts/README.md`](Scripts/README.md). |
| [`agents/`](agents/) | Prompt and parameter files for the four LLM-facing agents — see [`agents/README.md`](agents/README.md). |
| [`Platforms/`](Platforms/) | PDK-specific cell catalogs and OpenROAD preambles — see [`Platforms/README.md`](Platforms/README.md). |
| `.claudeignore` | Bounds what files the Claude CLI agents may autonomously read. |

The ORFS tree (`OpenROAD-flow-scripts/`) will be added as a git submodule
in a follow-up commit. Until then, place your own ORFS clone at that
path (or update `_ORFS_CFG.root_dir_name` in `run.py`).

## Prerequisites

- Python 3 (standard library only — no pip packages).
- Docker, with image `rsz_fix` available locally (custom build of ORFS;
  Dockerfile lives at `OpenROAD-flow-scripts/Dockerfile.rsz_fix` once the
  submodule is added).
- Claude CLI binary on `$PATH`, with model access to
  `claude-opus-4-7` (Planner / Selector) and `claude-sonnet-4-6`
  (Executor + retry).
- One or both PDKs available via the ORFS tree:
  ASAP7 at `OpenROAD-flow-scripts/flow/platforms/asap7/`,
  Nangate45 at `OpenROAD-flow-scripts/flow/platforms/nangate45/`.

## Running the flow

All commands run from this repository root. `run.py` is stage-based — pick a
single stage with `--run-stage`, or clean a stage with `--clean`.

```bash
# Stage 0 — CTS inside Docker (also copies 4_1_cts.odb → base/base_cts.odb)
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage base

# Stage 1 — Default repair_timing + GR (produces after-GR default.odb)
python3 run.py --design <design> --agent <agent> --pdk <asap7|nangate45> \
    --run-stage default

# Stage 2 — LLM iteration loop (Reporter / Planner / Executor / Selector)
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

### CLI arguments

| Flag | Required | Notes |
|------|----------|-------|
| `--design` | yes | Design name as used in `OpenROAD-flow-scripts/flow/designs/<pdk>/<design>/config.mk` |
| `--agent` | yes | Agent label (e.g. `claude`); namespaces work_dir output |
| `--pdk` | yes | `asap7` or `nangate45` |
| `--run-stage` | one of | `base`, `default`, `LLM-iterations`, `backend_default`, `backend_best`, `backend_rank2`, `backend_rank3`, `backend_rank4`, `all` |
| `--clean` | one of | `base`, `default`, `agentic_flow`, `all` |
| `--max-iterations` | no | LLM iterations upper bound (default 15) |
| `--start-iteration` | no | Resume LLM loop from this iteration (default 1) |
| `--claude-bin` | no | Path to Claude CLI binary (default: `claude`) |

Exactly one of `--clean` or `--run-stage` must be provided.

## Stage Semantics

- **`base_cts`** — raw post-CTS snapshot (`base/base_cts.odb`); no `repair_timing`,
  placement parasitics only.
- **`default`** — ORFS default `repair_timing` + legalization + global route
  (after-GR parasitics).
- **`plan<i>`** — Planner-chosen `repair_timing` moves + legalization + global
  route (after-GR parasitics). One per plan per iteration.

## Outputs

For design `D` under agent `A`:

- `work_dir/A/orfs_fix/<pdk>/D/base/`, `default/` — base and default-stage results
- `work_dir/A/orfs_fix/<pdk>/D/LLM_iterations/Iteration<N>/` — per-iteration
  planner / selector JSON + reporter prompt + `plan<i>/` subdirs + `best/`
- `work_dir/A/orfs_fix/<pdk>/D/{design}_baseline.csv`,
  `{design}_agentic[_r<N>].csv` — ledger rows for each backend stage
- `work_dir/A/orfs_fix/<pdk>/D/experiment_summary.csv` — per-iteration
  summary across plans
- `work_dir/A/orfs_fix/<pdk>/D/runtimes.csv`,
  `work_dir/A/orfs_fix/<pdk>/D/tokens.csv` — wall-time and LLM cost
- `run_logs/<design>_<pdk>/` — Docker / OpenROAD / make stdout+stderr per step

## Conventions

- `base_cts`, `default`, and `plan<i>` are distinct states and must never
  be conflated.
- Every loop run ends with Blocks A–D of ORFS `global_route.tcl` and reports
  after-GR metrics. Do not move `repair_timing` after `global_route` inside
  a plan — that belongs in the backend shim.
- The backend shim writes `5_1_grt.{odb,sdc,guide}` in ORFS's results layout
  so `make detail_route finish` consumes them.
