# `agents/`

Prompts and reference docs loaded by the LLM agents. Selector thresholds are
split into `PARAMETERS.md` so they can be tuned without touching the prompt.

| File | What it is |
|------|------------|
| `planner/AGENTS.md` | Planner agent prompt. Emits `planner_decision.json` with up to seven plans across the three plan types — sequence, single-operation fix, and targeted fix. |
| `executor/AGENTS.md` | Executor agent prompt — invoked only when a worker's `run_plan.tcl` errors inside Docker. First-pass TCL generation is `scripts/python/executor.py`. |
| `selector/AGENTS.md` | Selector agent prompt. Rates each plan (A/B/C/F), produces the stuck-path / avoid list, and picks one of `continue` / `retry` / `accept_regression` / `backtrack` / `stop`. |
| `selector/PARAMETERS.md` | Selector thresholds: plateau windows, backtrack trigger, regression budget, closure cutoff. |
| `openroad_reference.md` | OpenROAD `repair_timing` reference — nine operations + knob semantics. Every agent reads this. |
| `asap7.md` | ASAP7 cell-naming and V_t-tier reference, loaded when `--pdk asap7`. |
| `nangate45.md` | NanGate45 cell reference (single-V_t, no `vt_swap`), loaded when `--pdk nangate45`. |
