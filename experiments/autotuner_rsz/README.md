# autotuner_rsz

Optuna 3.x TPE search over the `repair_timing -setup` knob space. Sequence
is encoded as 18 parameters (`include_move` flag + `weight_move ∈ [1.0, 10.0]`
per operation); cost = 0.4·WNS + 0.4·TNS + 0.2·power. Independent trials,
all starting from the same `base_cts.odb`.

## Prerequisites

- `pip install optuna`
- Docker (the chosen ORFS image must be available locally)
- An ORFS tree at `openroad-flow-scripts/ (the submodule at the project root)`

## Run

```bash
python3 experiments/autotuner_rsz/run_autotuner.py --design aes --pdk asap7 --run-stage base
python3 experiments/autotuner_rsz/run_autotuner.py --design aes --pdk asap7 --run-stage tune \
    --n-startup-trials 20 --n-iterations 15 --n-jobs 4
python3 experiments/autotuner_rsz/run_autotuner.py --design aes --pdk asap7 --run-stage backend
```

Add `--resume` to continue an existing `optuna_study.db`.

## CLI flags

| Flag | Required | Choices / default | Purpose |
|------|----------|-------------------|---------|
| `--design` | yes | e.g. `aes`, `jpeg`, `ibex` | Design name. |
| `--pdk` | yes | `asap7` | Target PDK. |
| `--run-stage` | one of run-stage / clean | `base`, `tune`, `backend` | Single stage to run and exit. |
| `--clean` | one of run-stage / clean | `base`, `tune`, `backend`, `all` | Delete a stage's artifacts and exit. |
| `--n-startup-trials` | no | `20` | [tune] Random trials before TPE takes over. |
| `--n-iterations` | no | `15` | [tune] TPE-guided parallel iterations. |
| `--n-jobs` | no | `4` | [tune] Parallel trials per iteration. |
| `--resume` | no | — | [tune] Resume the existing `optuna_study.db`. |
| `--finalize` | no | — | [tune] Skip Phase 1/2; write summary files from the existing study. |
