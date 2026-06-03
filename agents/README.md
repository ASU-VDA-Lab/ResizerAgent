# `agents/`

Prompt and parameter files for the Claude-CLI agents invoked by the
iteration loop. The loop has four phases (**Report → Plan → Execute →
Select**); three of them delegate their reasoning step to an agent and so
have a prompt subdirectory below. The Report phase is pure Python and has
no agent. Shared reference docs (PDKs, OpenROAD command reference) live at
this level.

## Phase overview

```
Report   → Plan       → Execute              → Select
(Python)   (Planner     (Python first pass,    (Python pre-compute,
            agent)       Executor agent on      Selector agent)
                         TCL-error retry)
```

## Agent prompts

| Path | Phase | What it is |
|------|-------|------------|
| `planner/AGENTS.md` | Plan | **Planner agent** prompt. Reads the `reporter_baseline_prompt.txt` produced by the Report phase; emits `planner_decision.json` containing 1–7 execution plans (mix of `sequence`, `staged`, and `eco` types) plus rationale and expected outcomes. |
| `executor/AGENTS.md` | Execute | **Executor agent** prompt — invoked only on TCL-error retry. The first pass through the Execute phase is pure-Python `Scripts/python/executor.py`, which validates and emits `run_plan.tcl` for each plan. If a worker's OpenROAD run hits a TCL error, this agent repairs the script and the worker re-runs. |
| `selector/AGENTS.md` | Select | **Selector agent** prompt. Rates each plan's result, names the worst stuck path with cell-level guidance for the next Plan phase, and decides `continue` / `stop` / `backtrack`. `selector_prep.py` pre-computes all mechanical fields (rankings, regression flags, plateau / convergence diagnostics) so the agent focuses on semantic reasoning. |
| `selector/PARAMETERS.md` | Select | Tunable Selector thresholds (plateau detection, backtrack windows, etc.). `selector/AGENTS.md` defers to the values in this file. |

## Top-level reference docs

| File | Role |
|------|------|
| `openroad_reference.md` | OpenROAD `repair_timing` reference: nine moves (`sizeup`, `sizedown`, `vt_swap`, `buffer`, `split`, `unbuffer`, `clone`, `sizeup_match`, `swap`), knob semantics (`-cap_margin`, `-slew_margin`, `-max_buffer_percent`, etc.), Docker constants. **Every agent must read this before proposing a sequence, writing TCL, or interpreting repair results.** |
| `asap7.md` | ASAP7 cell-naming reference: `<FAMILY><drive>_ASAP7_<VT>` parse, VT tier ordering (RVT / LVT / SLVT), drive-strength ladder, unsizable families (HA, FA). Loaded by the Planner / Selector when `--pdk asap7`. |
| `nangate45.md` | Nangate45 / FreePDK45 cell reference: `<FAMILY>_X<size>` parse, single-VT (no `vt_swap` move possible), drive-strength ladder. Loaded by the Planner / Selector when `--pdk nangate45`. |

## Why there is no Reporter prompt

The Report phase is implemented entirely as a Python function
(`run.py:run_reporter()`) — it parses ODB / log artifacts, builds the
Plan-phase prompt, and never calls an LLM itself.

The Execute phase's first pass is similarly Python
(`Scripts/python/executor.py`); the `executor/AGENTS.md` prompt is only
invoked on the LLM retry path (when a worker's `run_plan.tcl` fails inside
Docker and needs an LLM-driven fix).
