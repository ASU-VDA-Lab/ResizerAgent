# ablation1_no_targeted_fixes  —  A1: No targeted fixes

Ablation A1 of ResizerAgent. The planner agent's targeted-fix plan type is
disabled — RA can only emit sequence and single-operation fix plans, i.e.,
modify the `repair_timing -setup` knobs.

## Run

From this directory:

```bash
# 1. Generate the post-CTS seed (one-time per design)
python3 run.py --design aes --agent claude --pdk asap7 --run-stage base

# 2. Run the ORFS default repair_timing baseline (BL reference)
python3 run.py --design aes --agent claude --pdk asap7 --run-stage default

# 3. Run the Report → Plan → Execute → Select loop
python3 run.py --design aes --agent claude --pdk asap7 \
    --run-stage LLM-iterations --max-iterations 15

# 4. (Optional) Push the loop's best ODB through detail routing
python3 run.py --design aes --agent claude --pdk asap7 --run-stage backend
```

Or chain everything with `--run-stage all`.

## CLI flags

| Flag | Required | Choices / default | Purpose |
|---|---|---|---|
| `--design` | yes | e.g. `aes`, `jpeg`, `ibex` | Design name; must exist under the chosen ORFS tree. |
| `--agent` | yes | e.g. `claude` | Agent label; tags the output tree (`work_dir/<agent>/<design>/<pdk>/`). |
| `--pdk` | yes | `asap7` \| `nangate45` | Target PDK. |
| `--run-stage` | one of run-stage / clean | `base`, `default`, `LLM-iterations`, `backend_default`, `backend_best`, `backend_rank2..4`, `backend`, `all` | Single stage to run and exit. |
| `--clean` | one of run-stage / clean | `base`, `default`, `agentic_flow`, `all` | Delete a stage's artifacts and exit. |
| `--max-iterations` | no | `15` | Iteration cap for the `LLM-iterations` stage. |
| `--start-iteration` | no | `1` | Resume the loop from a specific iteration. |
| `--claude-bin` | no | `claude` | Path to the Claude CLI binary. |
| `--orfs` | no | `fix` | Which ORFS tree to use (`ORFS_fix`). |

## Stage semantics

- **`base`** — `make cts` in Docker, copy `base_cts.odb`, extract placement-parasitic metrics. No `repair_timing`.
- **`default`** — apply ORFS's default `repair_timing` once; this is the **BL** (baseline) reference.
- **`LLM-iterations`** — the agentic loop: each iteration runs Report → Plan → Execute → Select up to `--max-iterations` times.
- **`backend*`** — push a selected ODB through `make route finish` for post-DR metrics.
