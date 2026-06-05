# ablation3_no_physical_feedback  —  A3: No physical feedback

Ablation A3 of ResizerAgent (paper §5.2). Physical information is removed
from agent inputs, including the post-evaluation feedback signal — the report
phase drops placement/routing context and the loop no longer measures with
post-global-route parasitics. Global route is run only in the backend stage
after the loop concludes.

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

# 4. (Required for QoR comparison) Push the loop's best ODB through detail routing
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
| `--orfs` | no | `old` \| `new` \| `fix` (default `old`) | Which ORFS tree to use: `ORFS_old`, `ORFS_new`, or `ORFS_fix`. |

## Stage semantics

- **`base`** — `make cts` in Docker, copy `base_cts.odb`, extract placement-parasitic metrics. No `repair_timing`.
- **`default`** — apply ORFS's default `repair_timing` once; this is the **BL** (baseline) reference. Measured at placement parasitics in this ablation.
- **`LLM-iterations`** — the agentic loop; all metrics inside the loop use placement parasitics (no GR).
- **`backend*`** — push a selected ODB through `make route finish` for post-DR metrics. Use this stage to obtain comparable QoR numbers against the RA control.
