# ResizerAgent

This is the GitHub repository for the implementation of ResizerAgent (RA). RA is an LLM-based timing optimization framework that drives
OpenROAD's Resizer through a closed-loop agentic flow with adaptive optimization strategies and targeted netlist modifications. At each iteration RA
observes timing reports, physical feedback, and prior optimization outcomes, then commits to `repair_timing` operations across three plan types: sequence
plan, single-operation fix plan, and targeted fix plan, to minimize user-provided metric on a post-CTS design.

## Requirements

- Python 3 (standard library only)
- Docker, with the `orfs_ra:latest` image available locally [Can be made available on request]
- Claude Code CLI on `$PATH`, authenticated with a **Claude Max subscription**,
  with model access to `claude-opus-4-7` (planner and selector agents) and
  `claude-sonnet-4-6` (executor agent)
- The `openroad-flow-scripts/` submodule (cloned with `--recurse-submodules`), which ships both the ORFS flow and its PDK platforms (ASAP7, NanGate45)

## Setup

### Part 1 - Clone the repository

Clone with submodules so the `openroad-flow-scripts/` ORFS tree (flow + ASAP7 /
NanGate45 platforms) is fetched:

```bash
git clone --recurse-submodules https://github.com/ASU-VDA-Lab/ResizerAgent.git
cd ResizerAgent
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init openroad-flow-scripts
```

The Docker image `orfs_ra:latest` must be built or loaded separately.

### Part 2 - Set up the Claude Code CLI

The agents are driven by the Claude Code CLI, which needs a **Claude Max
subscription** for the required model access. Install it, ensure it is on your
`$PATH`, and authenticate:

```bash
# Install (native binary → ~/.local/bin/claude)
curl -fsSL https://claude.ai/install.sh | bash

# If `which claude` is empty, add the install dir to your PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Verify
claude --version
```

Then authenticate your account following Anthropic's official guide -
<https://code.claude.com/docs/en/authentication>. Once `claude` is on `$PATH`
and authenticated, the flow can invoke it; pass a non-default binary with
`--claude-bin` if needed.

## Run

```bash
# Run the base stage where the Openroad Flow Scripts runs till CTS without performing post-CTS timing optimization
python3 run.py --design aes --agent claude --pdk asap7 --run-stage base

# Run the default repair stage where Openroad Flow Scripts runs the default timing repair 
python3 run.py --design aes --agent claude --pdk asap7 --run-stage default

# Run ResizerAgent to optimize timing
python3 run.py --design aes --agent claude --pdk asap7 \
    --run-stage LLM-iterations --max-iterations 15

# Run the backend flow to evaluate post detail-route PPA
python3 run.py --design aes --agent claude --pdk asap7 --run-stage backend
```

`--run-stage all` chains everything in one invocation.

## CLI flags

| Flag | Required | Choices / default | Purpose |
|------|----------|-------------------|---------|
| `--design` | yes | e.g. `aes`, `jpeg`, `ibex` | Design name under `openroad-flow-scripts/flow/designs/<pdk>/`. |
| `--agent` | yes | e.g. `claude` | Agent label; namespaces the output tree. |
| `--pdk` | yes | `asap7` \| `nangate45` | Target PDK. |
| `--run-stage` | one of run-stage / clean | `base`, `default`, `LLM-iterations`, `backend_default`, `backend_best`, `backend_rank2..4`, `backend`, `all` | Single stage to run and exit. |
| `--clean` | one of run-stage / clean | `base`, `default`, `agentic_flow`, `all` | Delete a stage's artifacts and exit. |
| `--max-iterations` | no | `15` | Iteration cap for `LLM-iterations`. |
| `--start-iteration` | no | `1` | Resume the loop from a specific iteration. |
| `--claude-bin` | no | `claude` | Path to the Claude CLI binary. |

## Output

Results land under `work_dir/<agent>/<pdk>/<design>/` with subdirectories
`base/`, `default/`, and `llm_iterations/iteration<N>/`, plus a cross-iteration
ranking at `llm_iterations/best_solutions/rankings.json`.

See `sample_output_structure/gcd_180_asap7/README.md` for a tour of a real run.
