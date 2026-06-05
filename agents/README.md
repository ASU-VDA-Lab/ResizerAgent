# `agents/`

Prompts and reference docs loaded by the LLM agents. Selector thresholds are
split into `PARAMETERS.md` so they can be tuned without touching the prompt.

| File | What it is |
|------|------------|
| `planner/AGENTS.md` | Planner agent system prompt. |
| `executor/AGENTS.md` | Executor agent system prompt (invoked only when a worker's `run_plan.tcl` errors inside Docker.) |
| `selector/AGENTS.md` | Selector agent system prompt. |
| `selector/PARAMETERS.md` | Selector thresholds: plateau windows, backtrack trigger, regression budget, closure cutoff. |
| `openroad_reference.md` | OpenROAD `repair_timing` reference: nine operations + knob semantics. |
| `asap7.md` | ASAP7 cell-naming and V_t-tier reference, loaded when `--pdk asap7`. |
| `nangate45.md` | NanGate45 cell reference, loaded when `--pdk nangate45`. |
