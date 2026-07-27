# Dataset

Design inputs for every paper configuration, plus the scripts to stage and
(re)build them. Each design point is stored **once** — the design files are
experiment-independent inputs, so there are no per-experiment copies.

## Layout
```
<pdk>/<design>/<design>_<util>_<cp>/
    design/   RTL source
    setup/    config.mk, SDC, rules, ...
```

- **pdk**: `asap7` | `nangate45`
- **design**: `aes` | `ibex` | `jpeg`
- **config**: `<util>_<cp>` — utilization (%) and clock period (ps), e.g. `70_430`

The set of configs per design is the union of the paper's points: utilization
sweep (Table 3 / Table 9), clock-period sweep (Fig 2 / Fig 3), and the ASAP7
ablation configs (Table 4). 36 configs total (22 asap7 + 14 nangate45).

## Scripts (in this directory)
- `design_setup.py` — stage one config into the ORFS flow tree.
- `populate_dataset.py` — (re)build the per-PDK data dirs from ORFS + the paper table.
- `run_autotuner_exp.py` — AutoTuner pipeline.

## How to run a config
```bash
# 1. Stage the design/config into the ORFS tree (copies files only)
python3 AE/dataset/design_setup.py --pdk asap7 --design jpeg --config 70_430

# 2. Run the ResizerAgent flow from the repo root
python3 run.py --design jpeg --pdk asap7 --agent claude --run-stage all
```

`--config` takes `<util>_<cp>` (the design prefix is added automatically).
Add `--dry-run` to `design_setup.py` to preview without copying.
