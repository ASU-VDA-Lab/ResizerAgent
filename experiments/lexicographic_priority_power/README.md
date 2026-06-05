# lexicographic_priority_power  —  Power-priority variant

Lexicographic-priority variant of ResizerAgent. The selector agent's ranking
policy Π_rank is reordered so power is ranked above WNS and TNS — same
architecture as RA, only the user-defined priority and the corresponding
ε_metric thresholds change.

## Run

From this directory:

```bash
# 1. Generate the post-CTS seed (one-time per design)
python3 run.py --design aes --agent claude --pdk asap7 --run-stage base

# 2. Run the ORFS default repair_timing baseline (BL reference)
python3 run.py --design aes --agent claude --pdk asap7 --run-stage default

# 3. Run the Report → Plan → Execute → Select loop (power-priority Π_rank)
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
| `--orfs` | no | `old` \| `new` \| `fix` (default `old`) | Which ORFS tree to use: `ORFS_old`, `ORFS_new`, or `ORFS_fix`. |

## Stage semantics

- **`base`** — `make cts` in Docker, copy `base_cts.odb`, extract placement-parasitic metrics. No `repair_timing`.
- **`default`** — apply ORFS's default `repair_timing` once; this is the **BL** (baseline) reference.
- **`LLM-iterations`** — the agentic loop with Π_rank = `power > WNS > TNS`. Among plans that hold timing within ε_WNS / ε_TNS, the selector picks the lowest-power one.
- **`backend*`** — push a selected ODB through `make route finish` for post-DR metrics.
