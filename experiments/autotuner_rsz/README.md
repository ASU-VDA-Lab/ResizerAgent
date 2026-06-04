# autotuner_rsz

Standalone Optuna TPE search over `repair_timing` sequence + knobs. Replaces the
LLM Planner with hyperparameter optimization. Trials are independent — each
starts from the same `base_cts.odb` (no ODB state carried forward).

## Prerequisites

- `pip install optuna` (only external Python dependency)
- Docker (the chosen ORFS image must be available locally)
- An ORFS tree at `experiments/autotuner_rsz/ORFS_fix/` when `--orfs fix`
  (default), or top-level `ORFS_old/` / `ORFS_new/` for the other variants

## Usage

```bash
# 1. Generate base_cts.odb (one-time per design)
python3 experiments/autotuner_rsz/run_autotuner.py \
    --design aes --pdk asap7 --run-stage base

# 2. Run the search
python3 experiments/autotuner_rsz/run_autotuner.py \
    --design aes --pdk asap7 --run-stage tune \
    --n-startup-trials 20 --n-iterations 15 --n-jobs 4

# 3. (Optional) Route + finish the best trial through ORFS
python3 experiments/autotuner_rsz/run_autotuner.py \
    --design aes --pdk asap7 --run-stage backend
```

Add `--resume` to a `tune` invocation to continue from `optuna_study.db`.
Run `python3 experiments/autotuner_rsz/run_autotuner.py --help` for the full
flag list and output layout.
