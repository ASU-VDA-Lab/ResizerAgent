# `agents/`

Prompts and parameters for the LLM agents that ResizerAgent (RA) drives. The
loop has four phases — **Report → Plan → Execute → Select** — and three of
them delegate their reasoning step to an agent. The Report phase is pure
Python and has no agent. Shared cell-naming and OpenROAD references live at
this level.

## Phase overview

```
Report   → Plan          → Execute               → Select
(Python)   (planner         (Python first pass,    (Python pre-compute,
            agent)           executor agent on      selector agent)
                             TCL-error retry)
```

## Agent prompts

| Path | Phase | What it is |
|------|-------|------------|
| `planner/AGENTS.md` | Plan | **Planner agent** prompt. Reads the structured prompt produced by the Report phase and emits `planner_decision.json` with up to seven candidate plans across the three plan types — **sequence plan** (`sequence`), **single-operation fix plan** (`staged`), and **targeted fix plan** (`eco`) — each with its operation list / knob settings, rationale, and expected outcome. |
| `executor/AGENTS.md` | Execute | **Executor agent** prompt — invoked only on TCL-error retry. The first pass through the Execute phase is pure-Python `scripts/python/executor.py`, which validates each plan and emits its `run_plan.tcl`. If a Docker worker's OpenROAD run hits a TCL error, this agent repairs the script and the worker re-runs. |
| `selector/AGENTS.md` | Select | **Selector agent** prompt. Rates each plan with an A/B/C/F label, produces a stuck-path list and avoid list for the next Plan phase, and chooses one flow decision: `continue`, `retry`, `accept_regression`, `backtrack`, or `stop`. `selector_prep.py` pre-computes the mechanical fields (lexicographic rankings, regression flags, plateau / convergence diagnostics) so the agent focuses on semantic reasoning. |
| `selector/PARAMETERS.md` | Select | Tunable selector thresholds (plateau windows, backtrack trigger, regression budget, closure cutoff). `selector/AGENTS.md` defers to the values in this file. |

## Top-level reference docs

| File | Role |
|------|------|
| `openroad_reference.md` | OpenROAD `repair_timing` reference: the nine timing operations (`sizeup`, `sizedown`, `vt_swap`, `buffer`, `split`, `unbuffer`, `clone`, `sizeup_match`, `swap`), knob semantics, and Docker constants. **Every agent reads this before proposing a sequence, writing TCL, or interpreting repair results.** |
| `asap7.md` | ASAP7 cell-naming reference: `<FAMILY><drive>_ASAP7_<VT>` parse, V_t tier ordering (RVT / LVT / SLVT), drive-strength ladder, and unsizable families (HA, FA). Loaded by the planner and selector agents. |

## Why there is no Reporter prompt

The Report phase is a pure Python function (`run.py:run_reporter()`) — it
parses ODB and log artifacts, builds the Plan-phase prompt, and never
invokes an LLM.

The Execute phase's first pass is similarly Python
(`scripts/python/executor.py`); the `executor/AGENTS.md` prompt is only
reached on the LLM retry path, when a worker's `run_plan.tcl` fails inside
Docker and needs an LLM-driven repair.
