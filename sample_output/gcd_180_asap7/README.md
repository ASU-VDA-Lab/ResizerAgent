# Sample Output — gcd_180 / asap7 (control)

A real `run.py` invocation produces the directory layout below. This sample is
the first iteration of an actual `gcd_180` run on `asap7` with the **control**
configuration (no ablations). It is meant as a tour of what each file is and
what to read first.

## What the control flow does

For one design, the control flow is:

1. **`base`** — run `make cts` in Docker via the chosen ORFS image, copy the raw
   post-CTS database (no `repair_timing` applied). Generates `base_*` reports.
2. **`default`** — apply ORFS's default `repair_timing` invocation once;
   record metrics as the baseline the LLM loop must beat.
3. **`LLM_iterations/Iteration<N>`** — for up to N iterations:
   - **Reporter** (pure Python) — parse the seed ODB's reports and assemble a
     prompt for the Planner.
   - **Planner** (Opus) — read the prompt, emit `planner_decision.json` with
     1–7 plans of types `sequence` / `staged` / `eco`.
   - **Executor** (Python TCL generator + per-plan Docker run) — turn each plan
     into a `run_plan.tcl` and execute it; collect metrics per plan.
   - **Selector** (Opus) — rate each plan, pick the best, emit
     `selector_decision.json` and seed the next iteration.
4. **`Best_solutions/rankings.json`** — running cross-iteration ranking of
   every successful plan by WNS.

The sample shown here is exactly what one design's results directory looks
like after Iteration1 finishes — everything Iteration2..N would mirror the
same shape under sibling `Iteration<N>/` directories.

## Top-level layout

```
gcd_180_asap7/
├── README.md
├── base/                    # Raw post-CTS — no repair_timing applied
├── default/                 # ORFS default repair_timing baseline
├── LLM_iterations/
│   ├── Iteration1/          # ← one full Report → Plan → Execute → Select cycle
│   └── Best_solutions/      # cross-iteration WNS ranking
├── run_logs/                # ORFS stage logs (CTS, default, base-artifacts)
├── apicall.csv              # LLM API call count per role per iteration
├── runtimes.csv             # wall-time per stage
└── tokens.csv               # LLM tokens + USD cost per call
```

Larger artifacts (`.odb`, netlist `.v`, full `_worst_paths.txt`) have been
stripped or truncated in this sample. The stub files note the original size.
Absolute host paths inside logs / CSVs / JSONs have been replaced with
`<project_root>` / `<sample_root>`.

## Stage 1 — `base/` (raw post-CTS)

| File | What it is |
|---|---|
| `base_cts.odb` | The post-CTS database that seeds every downstream step. **Stripped** in the sample. |
| `base_netlist.v` | Verilog dump of the post-CTS netlist. **Stripped**. |
| `base_timing.txt` | `WNS:` / `TNS:` numbers from `report_checks` at placement parasitics. |
| `base_area.rpt`, `base_power.rpt` | OpenROAD `report_design_area` / `report_power` output. |
| `base_worst_paths.txt` | Top-N worst paths (`report_checks ... -slack_max 0`). **Truncated**. |
| `base_placement_report.txt` | Bin-by-bin density + critical-path bbox snapshot. |
| `base_metrics.csv` | Single-row WNS/TNS/area/power/util digest. |
| `violating_endpoint.txt` | Count of endpoints with negative slack. |
| `constraint.sdc` | Post-CTS SDC (clock period, IO constraints). |
| `config.mk` | Copy of the ORFS design `config.mk` so the run is self-describing. |

## Stage 2 — `default/` (ORFS default baseline)

Same file set as `base/`, but the database (`default.odb`) is post-default-
`repair_timing` and parasitics are **post-GR** rather than placement. This
is the bar the LLM loop must beat. `default_gr_status.txt` records whether
the global route succeeded or fell back to estimated parasitics.

## Stage 3 — `LLM_iterations/Iteration1/`

```
Iteration1/
├── start/                       # SEED snapshot fed into this iteration
├── plan1/  plan2/  plan3/       # one subdir per plan the Planner emitted
├── best/                        # the plan the Selector picked
├── reporter_baseline_prompt.txt # full Planner-facing prompt
├── planner_decision.json        # the Planner's structured decision
├── planner_convo.jsonl          # raw Anthropic API turns (system+user+assistant)
├── selector_decision.json       # the Selector's structured decision
├── selector_convo.jsonl         # raw Anthropic API turns for the Selector
└── iteration_summary.csv        # per-plan WNS/TNS/area/util/power roll-up
```

### `start/` — what the iteration starts from

`seed.odb` is the ODB carried forward from the previous iteration's `best/`
(or from `default.odb` on Iteration1). `seed_*` files mirror the standard
metric set so the Reporter can diff against the prior baseline.

### `plan<N>/` — one Executor run per plan

Each `plan<N>/` is the artifact of one `run_plan.tcl` execution:

| File | What it is |
|---|---|
| `run_plan.tcl` | Auto-generated TCL: load seed ODB → `repair_timing -setup -sequence ... -repair_tns ...` → DPL → placement report → write ODB → global route → post-GR reports. |
| `input.odb`, `output.odb` | Pre/post repair_timing databases. **Stripped**. |
| `output.sdc` | SDC re-written after repair (in case constraints were tweaked). |
| `plan<N>.log` | Full OpenROAD stdout/stderr from this plan's Docker run. |
| `plan<N>_netlist.v` | Verilog dump of the post-repair netlist. **Stripped**. |
| `plan<N>_timing.txt` / `_area.rpt` / `_power.rpt` / `_worst_paths.txt` / `_placement_report.txt` | Standard post-repair metric set, post-GR parasitics. |
| `plan<N>_metrics.csv` | Single-row digest used by the Selector. |
| `plan_metrics.csv` | Identical to the above — kept for backward compatibility with older parsers. |
| `violating_endpoint.txt` | Negative-slack endpoint count. |

### `best/` — the Selector's pick

Mirror of the winning `plan<N>/` (the Selector's choice), with the metric
files re-prefixed `best_*` and `best_plan.txt` recording which plan number
was promoted. `output.odb` becomes `seed.odb` for Iteration2.

### Planner artifacts

**`planner_decision.json`** — top-level fields:
- `decision`: `"execute"` (proceed with the plans) or `"request_data"` (ask
  the Reporter for extra context first — `suggest_eco`, `deep_worst_paths`,
  `path_neighbors`, or `verbose_repair_summary`).
- `rationale`: free-text bullets the Planner used to justify the picks.
- `plan_count`, `plans`: 1–7 plans, each with:
  - `plan_type`: `"sequence"` | `"staged"` | `"eco"`
  - `sequence`: ordered list of moves from `sizeup`, `sizedown`, `vt_swap`,
    `buffer`, `split`, `unbuffer`, `clone`, `sizeup_match`, `swap`
  - `run_knobs`: `repair_tns`, `max_passes`, `setup_margin`, …
  - `reasoning`, `expected_outcome`

**`planner_convo.jsonl`** — one JSON object per line, the raw Anthropic API
exchange (system + user + assistant + tool-call turns). Useful for
reproducing or debugging the Planner's reasoning.

**`reporter_baseline_prompt.txt`** — the full prompt the Planner received,
including the design / PDK header, the seed metrics summary, the worst-paths
table, and the post-GR congestion summary. This is what the LLM was looking
at when it chose the plans.

### Selector artifacts

**`selector_decision.json`** — top-level fields:
- `decision`: `"promote"` | `"retry"` | `"backtrack"` | `"stop"`
- `stop_reason`, `backtrack_to_iteration`: populated when applicable
- `per_plan_ratings`: per-plan `rating` (A–F) + free-text `note`
- `strategy_note`: `stuck_paths`, `plateau_diagnosis`, `guidance` bullets that
  the next iteration's Planner sees verbatim

**`selector_convo.jsonl`** — raw Anthropic API exchange for the Selector.

### `iteration_summary.csv`

One row per plan with `wns`, `tns`, `area_um2`, `util_percent`, `power_mw`,
`violating_endpoints`, plus `status` / `error_type` / `first_error` if the
plan crashed and a path to the plan's log.

## `LLM_iterations/Best_solutions/rankings.json`

Cross-iteration WNS ranking — every successful plan from every iteration
competes (not just Selector winners). Each entry:

```json
{
  "iteration": 3, "plan": "plan1",
  "wns_ps": -63.954, "tns_ps": -1030.916,
  "odb": "<sample_root>/LLM_iterations/Iteration3/plan1/output.odb",
  "sdc": "<sample_root>/LLM_iterations/Iteration3/plan1/output.sdc",
  "odb_exists": true, "chosen": true, "rank": 1
}
```

`chosen: true` means the Selector promoted it that iteration. Other entries
are also viable candidates for later backtracking or for the final pick.

## `run_logs/`

ORFS stage logs (these run **outside** the LLM loop, once per design):

| File | What it is |
|---|---|
| `generate_cts.log` | ORFS `make cts` output that produced the post-CTS database. |
| `generate_base_artifacts.log` | OpenROAD session that produced `base/base_*` reports from `base_cts.odb`. |
| `run_default.log` | OpenROAD session for the ORFS default `repair_timing` baseline (`default/`). |

## Top-level tracking CSVs

| File | Columns | What it's for |
|---|---|---|
| `runtimes.csv` | `iteration, stage, runtime_s, started_at` | Wall-time per stage (reporter / planner / planN / selector / base / default). Use for performance breakdowns. |
| `tokens.csv` | `iteration, role, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cost_usd, duration_s, started_at` | LLM cost ledger. |
| `apicall.csv` | `iteration, role, model, api_calls` | Number of API turns per role per iteration (a request_data path causes >1 turn for the Planner). |

## Files produced by later phases (not in this sample)

A complete run also produces (after the LLM loop finishes):

- `experiment_summary.csv` — per-iteration best metrics ledger
- `<design>_baseline.csv` — single-row baseline numbers (base + default)
- `<design>_agentic.csv` (and `_r2`, `_r3` for backend re-routes) — final
  agentic numbers vs the baseline
- `final_comparison.csv` — three-row baseline / agentic / delta digest
- `rank<N>_comparison.csv` — for rank-2 / rank-3 ODBs that were also pushed
  through routing for sensitivity analysis
- `state.json` — best-branch history (only when multi-branch search is on)

This sample's source run stopped before that phase, so those files are absent.

## Reading order for first-time users

1. `LLM_iterations/Iteration1/reporter_baseline_prompt.txt` — what the LLM sees.
2. `LLM_iterations/Iteration1/planner_decision.json` — what it decided.
3. `LLM_iterations/Iteration1/plan1/run_plan.tcl` — what that decision becomes.
4. `LLM_iterations/Iteration1/iteration_summary.csv` — what came out.
5. `LLM_iterations/Iteration1/selector_decision.json` — how the loop reacts.
