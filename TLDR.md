# TL;DR — run one design end-to-end

Full flow for **aes** on **ASAP7** (base → default → 2 LLM iterations → backend).
Assumes the image is loaded, the submodule is present, and the Claude CLI is
authenticated (see `README.md`).

```bash
# 1. Stage the design/config into the ORFS tree
python3 AE/dataset/design_setup.py --pdk asap7 --design aes --config 70_240

# 2. Run the stages (base + default need no API; LLM-iterations + backend do)
python3 run.py --design aes --pdk asap7 --agent claude --run-stage base
python3 run.py --design aes --pdk asap7 --agent claude --run-stage default
python3 run.py --design aes --pdk asap7 --agent claude --run-stage LLM-iterations --max-iterations 2
python3 run.py --design aes --pdk asap7 --agent claude --run-stage backend
```

Shortcut for all four: `--run-stage all --max-iterations 2`.

## What gets generated, and where

Everything lands under **`work_dir/<agent>/<pdk>/<design>/`** — here
`work_dir/claude/asap7/aes/`:

```
work_dir/claude/asap7/aes/
├── base/                 # post-CTS snapshot (no repair)
├── default/              # ORFS default repair_timing  = Baseline
├── llm_iterations/       # the ResizerAgent loop
│   ├── iteration1/  iteration2/
│   └── best_solutions/
├── apicall.csv  tokens.csv  runtimes.csv
└── README.md
```

| Stage | Directory | Key files | Meaning |
|-------|-----------|-----------|---------|
| **base** | `base/` | `base_metrics.csv` | WNS/TNS/area/util/power at post-CTS (`wns_ps,tns_ps,area_um2,util_pct,power_W`) |
| | | `base_timing.txt`, `base_worst_paths.txt`, `violating_endpoint.txt` | timing report + worst paths + failing endpoints |
| | | `base_area.rpt`, `base_power.rpt`, `config.mk`, `constraint.sdc` | area/power reports + the exact snapshot config |
| **default** | `default/` | `default_metrics.csv` | Baseline result after ORFS default `repair_timing` |
| | | `default_timing.txt`, `default_*.rpt`, `default_gr_status.txt` | reports + global-route status |
| **LLM-iterations** | `llm_iterations/iteration<N>/` | `start/` | metrics the iteration starts from |
| | | `plan1/ plan2/ plan3/` | the 3 candidate repair plans the Planner proposed |
| | | `plan<k>/run_plan.tcl` | the exact `repair_timing` sequence tried |
| | | `plan<k>/plan<k>_metrics.csv`, `plan<k>_timing.txt`, `output.sdc` | that plan's WNS/TNS/area/power + reports + resulting SDC |
| | | `best/` | the plan the Selector picked for this iteration |
| | `llm_iterations/best_solutions/` | `rankings.json` | cross-iteration ranking of the best plans (lowest WNS) |
| | (design root) | `apicall.csv`, `tokens.csv`, `runtimes.csv` | LLM call log, token usage, per-stage timings |
| **backend** | (design root) | post-DR PPA for the default and the best RA plan (and ranks) | final **post-detailed-route** WNS/TNS/area/power |

**How to read the result:** compare `default/default_metrics.csv` (Baseline)
against the best plan in `llm_iterations/best_solutions/rankings.json` (RA), at
post-Opt (in the metrics CSVs) and post-DR (after `backend`). RA should improve
or match WNS/TNS at comparable area/power.
