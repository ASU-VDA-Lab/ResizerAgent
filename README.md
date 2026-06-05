# ResizerAgent

This is the github repository for the implementation of ResizerAgent(RA). RA is an LLM-based timing optimization framework that drives
OpenROAD's Resizer through a closed-loop agentic flow with adaptive optimization strategies and targeted netlist modifications. At each iteration RA
observes timing reports, physical feedback, and prior optimization outcomes,
then commits to `repair_timing` operations across three plan types — sequence
plan, single-operation fix plan, and targeted fix plan — to minimize worst
negative slack (WNS) and total negative slack (TNS) on a post-CTS design.

## Requirements

- Python 3 (standard library only)
- Docker, with the `orfs_ra:latest` image available locally [Can be made available on request]
- Claude CLI on `$PATH`, with model access to `claude-opus-4-7` (planner and
  selector agents) and `claude-sonnet-4-6` (executor agent)
- ASAP7 PDK from the bundled `openroad-flow-scripts/` submodule

## Setup

```bash
git clone --recurse-submodules https://github.com/ASU-VDA-Lab/ResizerAgent.git
cd ResizerAgent
```

The Docker image `orfs_ra:latest` must be built or loaded separately.

## Run

```bash
python3 run.py --design aes --agent claude --pdk asap7 --run-stage base
python3 run.py --design aes --agent claude --pdk asap7 --run-stage default
python3 run.py --design aes --agent claude --pdk asap7 \
    --run-stage LLM-iterations --max-iterations 15
python3 run.py --design aes --agent claude --pdk asap7 --run-stage backend
```

`--run-stage all` chains everything in one invocation.

## CLI flags

| Flag | Required | Choices / default | Purpose |
|------|----------|-------------------|---------|
| `--design` | yes | e.g. `aes`, `jpeg`, `ibex` | Design name under `openroad-flow-scripts/flow/designs/<pdk>/`. |
| `--agent` | yes | e.g. `claude` | Agent label; namespaces the output tree. |
| `--pdk` | yes | `asap7` | Target PDK. |
| `--run-stage` | one of run-stage / clean | `base`, `default`, `LLM-iterations`, `backend_default`, `backend_best`, `backend_rank2..4`, `backend`, `all` | Single stage to run and exit. |
| `--clean` | one of run-stage / clean | `base`, `default`, `agentic_flow`, `all` | Delete a stage's artifacts and exit. |
| `--max-iterations` | no | `15` | Iteration cap for `LLM-iterations`. |
| `--start-iteration` | no | `1` | Resume the loop from a specific iteration. |
| `--claude-bin` | no | `claude` | Path to the Claude CLI binary. |

## Output

Results land under `work_dir/<agent>/<pdk>/<design>/` with subdirectories
`base/`, `default/`, and `llm_iterations/iteration<N>/`, plus a cross-iteration
ranking at `llm_iterations/best_solutions/rankings.json`.

See `sample_output/gcd_180_asap7/README.md` for a tour of a real run.
