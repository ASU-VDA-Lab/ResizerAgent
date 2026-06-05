# lexicographic_priority_power 

Reorders the selector's ranking policy so power is ranked above WNS and TNS.

## Run

```bash
python3 run.py --design aes --agent claude --pdk asap7 --run-stage base
python3 run.py --design aes --agent claude --pdk asap7 --run-stage default
python3 run.py --design aes --agent claude --pdk asap7 --run-stage LLM-iterations --max-iterations 15
python3 run.py --design aes --agent claude --pdk asap7 --run-stage backend
```

`--run-stage all` chains everything.

## CLI flags

| Flag | Required | Choices / default | Purpose |
|------|----------|-------------------|---------|
| `--design` | yes | e.g. `aes`, `jpeg`, `ibex` | Design name. |
| `--agent` | yes | e.g. `claude` | Agent label; tags `work_dir/<agent>/…`. |
| `--pdk` | yes | `asap7` | Target PDK. |
| `--run-stage` | one of run-stage / clean | `base`, `default`, `LLM-iterations`, `backend_default`, `backend_best`, `backend_rank2..4`, `backend`, `all` | Single stage to run and exit. |
| `--clean` | one of run-stage / clean | `base`, `default`, `agentic_flow`, `all` | Delete a stage's artifacts and exit. |
| `--max-iterations` | no | `15` | Iteration cap for `LLM-iterations`. |
| `--start-iteration` | no | `1` | Resume from a specific iteration. |
| `--claude-bin` | no | `claude` | Path to the Claude CLI binary. |
| `--orfs` | no | `fix` | ORFS tree (`ORFS_fix`). |
