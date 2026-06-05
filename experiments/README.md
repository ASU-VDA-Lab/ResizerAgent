# experiments/

Companion configurations to **ResizerAgent (RA)** — the LLM-based timing
optimization framework that drives OpenROAD's Resizer through a closed-loop
agentic flow (*Report → Plan → Execute → Select*). The full RA system lives at
the repo root; each subdirectory here is either an **ablation** (paper §5.2)
or a **lexicographic-priority variant** (paper §5.3) of RA, plus a local
build of the **AutoTuner baseline (AT)** used in the evaluation.

## Ablations (paper §5.2)

### `ablation1_no_targeted_fixes`  (paper: **A1 — No targeted fixes**)
Disables the targeted-fix plan type. RA can only emit sequence plans and
single-operation fix plans — i.e., modify the `repair_timing -setup` knobs in
Table 1 — with no explicit ECO-style edits.

### `ablation2_single_candidate_plan`  (paper: **A2 — Single candidate plan**)
Restricts the planner agent to generating exactly one candidate plan per
iteration. All three plan types remain available; only the multi-plan
exploration is removed.

### `ablation3_no_physical_feedback`  (paper: **A3 — No physical feedback**)
Removes physical information from agent inputs, including the post-evaluation
feedback signal — i.e., the report phase drops placement/routing context and
the loop no longer measures with post-global-route parasitics.

## Lexicographic-priority variant (paper §5.3)

### `lexicographic_priority_power`
Reorders the selector's lexicographic ranking policy (Π_rank) so power is
ranked above WNS and TNS. Same architecture as RA; only the user-defined
priority and the corresponding ε_metric thresholds change.

## AutoTuner baseline (paper §4)

### `autotuner_rsz`
Local build of the **AT** baseline. An Optuna 3.x TPE sampler tunes the same
Table 1 knobs RA controls (sequence encoded as 18 parameters: a binary
`include_move` flag and a continuous `weight_move ∈ [1.0, 10.0]` per
operation). No planner or selector agent — the cost function is a weighted
sum of WNS, TNS, and power (weights 0.4 / 0.4 / 0.2).

## Layout

Every ablation and the lexicographic variant mirror RA's layout (`run.py`,
`scripts/`, `agents/`, `platforms/`). `autotuner_rsz/` has its own
`run_autotuner.py` + stage scripts and a local `README.md` with the CLI.

See `../sample_output/gcd_180_asap7/README.md` for a per-file tour of what one
RA run produces.
