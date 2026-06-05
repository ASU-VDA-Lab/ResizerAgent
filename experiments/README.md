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
Local build of the autotuner baseline. 

