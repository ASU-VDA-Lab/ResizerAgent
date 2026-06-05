#!/usr/bin/env python3
"""
generate_cts.py — Stage 0: run ORFS make cts-a inside Docker.

Runs the OpenROAD-flow-scripts CTS stage for a given design inside the
Docker image so that 4_1_cts.odb / 4_1_cts.sdc land in:
  OpenROAD-flow-scripts/flow/results/asap7/<design>/base/

make clean_all is always run before make cts-a to ensure a clean build.

Usage (on host):
    python3 scripts/generate_cts.py --design <design_name> --agent <agent_name>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical path layout — all derived from this file's location
# ---------------------------------------------------------------------------
# This file lives at:  <workspace>/scripts/python/generate_cts.py
# So workspace root is three levels up.
WORKSPACE = Path(__file__).resolve().parent.parent.parent
WORK_DIR  = WORKSPACE / "work_dir"

DOCKER_IMAGE    = "openroad/flow-ubuntu22.04-builder:0b569c"
DOCKER_OPENROAD = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"
DOCKER_YOSYS    = "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"

# Inside Docker the workspace is mounted at /workspace.
# ORFS flow directory (inside Docker)
DOCKER_FLOW_HOME = "/workspace/OpenROAD-flow-scripts/flow"


def docker_run(design: str, log_path: Path) -> None:
    """Run make clean_all + make cts-a inside Docker for the given design."""
    design_config = f"./designs/asap7/{design}/config.mk"

    make_vars = (
        f"DESIGN_CONFIG={design_config} "
        f"OPENROAD_EXE={DOCKER_OPENROAD} "
        f"YOSYS_EXE={DOCKER_YOSYS} "
        f"ENABLE_POST_CTS_CLI_REPAIR_TIMING=0 "
        f"SKIP_CTS_REPAIR_TIMING=1"
    )

    shell_cmd = (
        f"make clean_all {make_vars} && "
        f"make cts-a {make_vars}"
    )

    cmd = [
        "docker", "run", "--rm",
        "--user", "root",
        "-e", f"FLOW_HOME={DOCKER_FLOW_HOME}",
        "-v", f"{WORKSPACE}:/workspace",
        "-w", "/workspace/OpenROAD-flow-scripts/flow",
        DOCKER_IMAGE,
        "/bin/bash", "-c", shell_cmd,
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Running ORFS CTS for design '{design}' → log: {log_path}")
    print(f"  Shell cmd inside Docker: {shell_cmd}")

    with open(log_path, "w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode

    if rc != 0:
        sys.exit(
            f"[ERROR] Docker make cts-a failed (exit {rc}). See {log_path}"
        )

    # Verify output exists
    expected = (
        WORKSPACE
        / "OpenROAD-flow-scripts"
        / "flow"
        / "results"
        / "asap7"
        / design
        / "base"
        / "4_1_cts.odb"
    )
    if not expected.exists():
        sys.exit(
            f"[ERROR] Expected CTS output not found: {expected}\n"
            f"        Check {log_path} for details."
        )
    print(f"  [OK] 4_1_cts.odb generated at {expected.parent}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run ORFS make cts-a inside Docker to generate 4_1 artifacts."
    )
    ap.add_argument("--design", required=True, help="Design name (e.g. aes)")
    ap.add_argument("--agent",  required=True, help="Agent name (e.g. claude, codex)")
    args = ap.parse_args()

    log_path = WORK_DIR / args.agent / args.design / "run_logs" / "generate_cts.log"

    print(f"\n[generate_cts] Building CTS artifacts for design '{args.design}'")
    docker_run(args.design, log_path)
    print(f"\n[Done] CTS artifacts ready. Run prepare_design.py next.")
    print(f"  logs → {log_path}")


if __name__ == "__main__":
    main()
