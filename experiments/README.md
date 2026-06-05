# experiments/


## Ablations

### `ablation1_no_targeted_fixes` 
Disables the targeted-fix plan type. RA can only emit sequence plans and
single-operation fix plans.

### `ablation2_single_candidate_plan` 
Restricts the planner agent to generating exactly one candidate plan per
iteration. All three plan types remain available; only the multi-plan
exploration is removed.

### `ablation3_no_physical_feedback` 
Removes physical information from agent inputs, including the post-evaluation
feedback signal, i.e., the report phase drops placement/routing context and
the loop no longer measures with post-global-route parasitics.

## Lexicographic-priority variant

### `lexicographic_priority_power`
Reorders the selector's lexicographic ranking policy so power is
ranked above WNS and TNS. Same architecture as RA; only the user-defined
priority and the corresponding metric thresholds change.

## AutoTuner baseline
### `autotuner_rsz`
Local build of the autotuner baseline. An Optuna 3.x TPE sampler tunes the same
knobs RA controls (sequence encoded as 18 parameters: a binary
`include_move` flag and a continuous `weight_move ∈ [1.0, 10.0]` per
operation). The cost function is a weighted
sum of WNS, TNS, and power (weights 0.4 / 0.4 / 0.2).

