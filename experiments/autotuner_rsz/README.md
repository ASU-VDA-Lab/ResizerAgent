# autotuner_rsz  —  AT baseline

Local build of the **AutoTuner (AT) baseline** for ResizerAgent. An
Optuna 3.x TPE sampler tunes the same `repair_timing -setup` knobs RA
controls. The sequence parameter is encoded as 18 parameters — one binary
`include_move` flag and one continuous `weight_move ∈ [1.0, 10.0]` per
operation — and the cost function is a weighted sum of WNS, TNS, and power
(weights 0.4 / 0.4 / 0.2). No planner or selector agent; trials are
independent and start from the same `base_cts.odb`.

## Prerequisites

- `pip install optuna` (only external Python dependency)
- Docker (the chosen ORFS image must be available locally)
- An ORFS tree at `experiments/autotuner_rsz/ORFS_fix/`

## Run

From the repo root:

```bash
# 1. Generate base_cts.odb (one-time per design)
python3 experiments/autotuner_rsz/run_autotuner.py \
    --design aes --pdk asap7 --run-stage base

# 2. Run the TPE search
python3 experiments/autotuner_rsz/run_autotuner.py \
    --design aes --pdk asap7 --run-stage tune \
    --n-startup-trials 20 --n-iterations 15 --n-jobs 4

# 3. (Optional) Route + finish the best trial through ORFS
python3 experiments/autotuner_rsz/run_autotuner.py \
    --design aes --pdk asap7 --run-stage backend
```

Add `--resume` to a `tune` invocation to continue from `optuna_study.db`.

## CLI flags

| Flag | Required | Choices / default | Purpose |
|---|---|---|---|
| `--design` | yes | e.g. `aes`, `jpeg`, `ibex` | Design name; must exist under the chosen ORFS tree. |
| `--pdk` | yes | `asap7` \| `nangate45` | Target PDK. |
| `--orfs` | no | `fix` (default) | Which ORFS tree / Docker image to use (`ORFS_fix`). |
| `--run-stage` | one of run-stage / clean | `base`, `tune`, `backend` | Single stage to run and exit. |
| `--clean` | one of run-stage / clean | `base`, `tune`, `backend`, `all` | Delete a stage's artifacts and exit. |
| `--n-startup-trials` | no | `20` | [tune] Sequential random trials before TPE takes over. |
| `--n-iterations` | no | `15` | [tune] TPE-guided parallel iterations after warmup. |
| `--n-jobs` | no | `4` | [tune] Parallel trials per iteration. |
| `--resume` | no | — | [tune] Resume the existing `optuna_study.db` rather than starting fresh. |
| `--finalize` | no | — | [tune] Skip Phase 1/2; just write summary files from the existing study (use after a crash that lost the wrap-up). |

## Stage semantics

- **`base`** — `make cts` in Docker, copy `base_cts.odb` + SDC, extract placement-parasitic metrics. Every trial seeds from this.
- **`tune`** — Optuna TPE search over the knob space. Each trial runs `repair_timing -setup` in Docker, measures WNS/TNS/area/power at placement parasitics, and reports the weighted score back to the sampler.
- **`backend`** — push the best trial's ODB through `make route finish` for post-DR metrics, matching how the RA loop's best ODB is finished.
