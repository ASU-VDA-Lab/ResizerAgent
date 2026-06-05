# experiments/

Variants of the control 4-phase flow (Report → Plan → Execute → Select) that
ships at the repo root. Each subdirectory is a self-contained copy with a
single design choice changed, so its results can be compared directly against
the control on the same design / PDK / iteration budget.

## Variants

### `ablation1_no_targeted_fixes`
Drops the `eco` plan type from the Planner schema and prompt. The Planner can
only emit `sequence` and `staged` plans — no per-instance surgical fixes —
isolating how much value targeted ECO edits add on top of bulk `repair_timing`.

### `ablation2_single_candidate_plan`
Constrains `plan_count` to exactly 1 per iteration (all three plan types still
allowed). Iteration cap is unchanged, so this tests focused depth (15 × 1)
against the control's wide search (up to 15 × 7).

### `ablation3_no_physical_feedback`
Measures WNS/TNS/area/power at **placement parasitics** instead of post-global-
route. The LLM loop never sees GR-aware numbers; ORFS handles GR + detail
route only in the backend stage after the loop concludes. Tests how much GR
feedback inside the loop matters.

### `lexicographic_priority_power`
Re-ranks the objective: the Selector prefers the lowest-power plan among
those that hold WNS within ~10 ps of seed. Planner prompt rewritten around
power-reducing moves (`unbuffer`, `sizedown`). Tests power-aware
timing closure under the same iteration budget.

### `autotuner_rsz`
Replaces the LLM Planner with an Optuna TPE search over the same
`repair_timing` sequence + knobs. No reasoning, no Selector — independent
trials starting from the same `base_cts.odb`. Baseline for "what would a
classical hyperparameter optimizer get on this problem."

## Layout

Every variant mirrors the control's layout (`run.py` at the top, `scripts/`,
`agents/`, `platforms/`) except `autotuner_rsz`, which has its own
`run_autotuner.py` + stage scripts and a local `README.md` with its CLI.

See `../sample_output/gcd_180_asap7/README.md` for a per-file tour of what one
run produces.
