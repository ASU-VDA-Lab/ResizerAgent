# ResizerAgent

A multi-agent CLI-driven OpenROAD timing-closure framework. Four agents
(**Reporter → Planner → Executor → Selector**) iterate over a post-CTS design
to minimize Worst Negative Slack (WNS) and Total Negative Slack (TNS). Every
iteration runs `repair_timing` plus global route inside the loop, and the
reported metrics are after-GR slack.

## The Four-Agent Loop

```
run.py (orchestrator)
  └─ Per iteration:
       Reporter  — pure Python; builds reporter_baseline_prompt.txt from
                   base_cts / default / prev-best artifacts (metrics, paths,
                   neighbors, cell catalog, GR metrics).
       Planner   — Claude CLI (Opus); reads reporter prompt, writes
                   planner_decision.json (1–7 plans: sequence / staged / eco).
       Executor  — pure Python; validates the planner JSON, emits one
                   run_plan.tcl per plan, then launch_workers.py runs them
                   in parallel inside Docker.
       Selector  — Claude CLI (Opus); rates plans, promotes the best to
                   Iteration<N>/best/, decides continue / stop / backtrack.
  └─ The promoted plan's after-GR ODB becomes the seed for the next
     iteration.
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
