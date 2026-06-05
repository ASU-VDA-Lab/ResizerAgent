# Sample Output — gcd_180 / asap7

One real iteration of a `gcd_180` run on `asap7`. Iteration2..N would mirror
the same shape under sibling `Iteration<N>/` directories.

Larger artifacts (`.odb`, `.v`, full `_worst_paths.txt`) are stripped or
truncated; stubs note the original size. Host paths in logs / CSVs / JSONs
are rewritten as `<project_root>` / `<sample_root>`.

## Top-level layout

```
gcd_180_asap7/
├── base/                # raw post-CTS, no repair_timing
├── default/             # ORFS default repair_timing baseline (BL)
├── LLM_iterations/
    ├── Iteration1/      # one full Report → Plan → Execute → Select cycle
    └── Best_solutions/  # cross-iteration WNS ranking
├── run_logs/            # ORFS stage logs
├── apicall.csv          # API calls per role per iteration
├── runtimes.csv         # wall-time per stage
└── tokens.csv           # LLM tokens + USD per call
```

## `base/` and `default/`

| File | What it is |
|------|------------|
| `base_cts.odb` / `default.odb` | Post-CTS / post-default-repair database. **Stripped**. |
| `base_netlist.v` / `default_netlist.v` | Post-CTS / post-repair netlist. **Stripped**. |
| `*_timing.txt`, `*_area.rpt`, `*_power.rpt` | OpenROAD `report_checks` / `report_design_area` / `report_power` output. |
| `*_worst_paths.txt` | Top-N worst paths. **Truncated**. |
| `*_placement_report.txt` | Density + critical-path bbox snapshot. |
| `*_metrics.csv` | Single-row WNS/TNS/area/power/util digest. |
| `violating_endpoint.txt` | Count of endpoints with negative slack. |
| `constraint.sdc`, `config.mk` | Post-CTS SDC and ORFS design `config.mk`. |
| `default_gr_status.txt` | (default only) Whether GR succeeded or fell back to estimates. |

`base/` is measured at placement parasitics; `default/` at post-GR.

## `LLM_iterations/Iteration1/`

```
Iteration1/
├── start/                       # seed snapshot fed into this iteration
├── plan1/  plan2/  plan3/       # one subdir per plan
├── best/                        # the plan the selector promoted
├── reporter_baseline_prompt.txt # prompt sent to the planner agent
├── planner_decision.json        # planner agent's structured decision
├── planner_convo.jsonl          # raw Anthropic API turns
├── selector_decision.json       # selector agent's structured decision
├── selector_convo.jsonl         # raw Anthropic API turns
└── iteration_summary.csv        # per-plan WNS/TNS/area/util/power roll-up
```

`start/seed.odb` is `default.odb` on Iteration1, otherwise the previous
iteration's `best/output.odb`.

Each `plan<N>/` is one `run_plan.tcl` execution:

| File | What it is |
|------|------------|
| `run_plan.tcl` | Auto-generated TCL: load seed → `repair_timing` → DPL → write ODB → GR → post-GR reports. |
| `input.odb`, `output.odb` | Pre/post `repair_timing` databases. **Stripped**. |
| `output.sdc` | SDC after repair. |
| `plan<N>.log` | OpenROAD stdout/stderr. |
| `plan<N>_netlist.v` | Post-repair netlist. **Stripped**. |
| `plan<N>_timing.txt` / `_area.rpt` / `_power.rpt` / `_worst_paths.txt` / `_placement_report.txt` | Post-GR metric set. |
| `plan<N>_metrics.csv`, `plan_metrics.csv` | Single-row digest (the second is a back-compat duplicate). |
| `violating_endpoint.txt` | Negative-slack endpoint count. |

`best/` mirrors the winning plan with files re-prefixed `best_*`;
`best_plan.txt` records which plan number was promoted, and its `output.odb`
becomes Iteration2's `seed.odb`.

`planner_decision.json` / `selector_decision.json` follow the schemas under
`scripts/schemas/`; `*_convo.jsonl` is the raw Anthropic API exchange for
that agent.

## `Best_solutions/rankings.json`

Cross-iteration WNS ranking — every successful plan competes, not just
selector winners. Entries carry `wns_ps`, `tns_ps`, paths to `odb`/`sdc`.

## `run_logs/`

| File | What it is |
|------|------------|
| `generate_cts.log` | ORFS `make cts` that produced `base_cts.odb`. |
| `generate_base_artifacts.log` | OpenROAD session that produced `base/base_*` reports. |
| `run_default.log` | OpenROAD session for the BL baseline. |

## Top-level CSVs

| File | What it tracks |
|------|----------------|
| `runtimes.csv` | Wall-time per stage (report/plan/planN/select/base/default). |
| `tokens.csv` | LLM tokens + USD cost per call. |
| `apicall.csv` | API turns per role per iteration. |


