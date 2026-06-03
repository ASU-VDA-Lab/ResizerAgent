# `agents/`

Prompt and parameter files for the four agents in the iteration loop.
**Two of these agents are pure Python (Reporter, Executor) and have no prompt
file here** — they live in `Scripts/python/`. The two LLM-driven agents
(Planner, Selector) plus the LLM-driven side of the Executor each have a
prompt subdirectory below. Shared reference docs (PDKs, OpenROAD command
reference) live at this level.

## Loop overview

```
Reporter  → Planner  → Executor  → workers  → Selector  → promote
(Python)    (LLM)      (Python +    (TCL +     (Python +     (Python)
                       LLM retry)   OpenROAD)  LLM)
```

## Subdirectories — LLM prompts

| Path | What it is |
|------|------------|
| `planner/AGENTS.md` | **Planner** prompt. Reads `reporter_baseline_prompt.txt` produced by the Python Reporter; emits `planner_decision.json` containing 1–7 execution plans (mix of `sequence`, `staged`, and `eco` types) plus rationale and expected outcomes. |
| `executor/AGENTS.md` | **Executor** prompt — used only on TCL-error retry. The first pass through is the pure-Python `Scripts/python/executor.py`, which validates and emits `run_plan.tcl` for each plan. If a worker's OpenROAD run hits a TCL error, this LLM prompt is invoked to repair the script. |
| `selector/AGENTS.md` | **Selector** prompt. Rates each plan's result, names the worst stuck path with cell-level guidance for the next Planner iteration, and decides `continue` / `stop` / `backtrack`. The Python `selector_prep.py` pre-computes all mechanical fields (rankings, regression flags, plateau / convergence diagnostics) so the LLM focuses on semantic reasoning. |
| `selector/PARAMETERS.md` | Tunable Selector thresholds (plateau detection, backtrack windows, etc.). `selector/AGENTS.md` defers to the values in this file. |

## Top-level reference docs

| File | Role |
|------|------|
| `openroad_reference.md` | OpenROAD `repair_timing` reference: nine moves (`sizeup`, `sizedown`, `vt_swap`, `buffer`, `split`, `unbuffer`, `clone`, `sizeup_match`, `swap`), knob semantics (`-cap_margin`, `-slew_margin`, `-max_buffer_percent`, etc.), Docker constants. **Every agent must read this before proposing a sequence, writing TCL, or interpreting repair results.** |
| `asap7.md` | ASAP7 cell-naming reference: `<FAMILY><drive>_ASAP7_<VT>` parse, VT tier ordering (RVT / LVT / SLVT), drive-strength ladder, unsizable families (HA, FA). Loaded by the Planner / Selector when `--pdk asap7`. |
| `nangate45.md` | Nangate45 / FreePDK45 cell reference: `<FAMILY>_X<size>` parse, single-VT (no `vt_swap` move possible), drive-strength ladder. Loaded by the Planner / Selector when `--pdk nangate45`. |

## Where the Reporter / Executor prompts live

There is intentionally no `agents/reporter/` directory. The Reporter is implemented entirely as a Python function (`run.py:run_reporter()`) — it parses ODB / log artifacts, builds the prompt for the Planner, and never calls an LLM itself.

Likewise the **first-pass** Executor is `Scripts/python/executor.py` — it validates `planner_decision.json` and emits `run_plan.tcl` for each plan deterministically. The `executor/AGENTS.md` prompt here is invoked only on the LLM retry path (when a worker's `run_plan.tcl` fails inside Docker and needs an LLM-driven fix).
