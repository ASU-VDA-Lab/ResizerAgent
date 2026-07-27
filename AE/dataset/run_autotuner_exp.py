#!/usr/bin/env python3
"""
run_autotuner_exp.py — Run the AutoTuner (AT) baseline on a given
(pdk, design, config). Stages the config into the standalone autotuner ORFS
tree, then runs the AT pipeline: base -> tune -> backend.

AT is standalone: its own ORFS tree (experiments/autotuner_rsz/openroad-flow-scripts)
and its own results (experiments/autotuner_rsz/results/<pdk>/<design>/).

Prereqs: `pip install optuna` (already installed), Docker image orfs_ra:latest.

Examples:
    # full pipeline on one config
    python3 AE/run_autotuner_exp.py --pdk asap7 --design ibex --config 70_650

    # just stage + base (no search yet)
    python3 AE/run_autotuner_exp.py --pdk asap7 --design ibex --config 70_650 --stage base

    # short search for a smoke test
    python3 AE/run_autotuner_exp.py --pdk asap7 --design aes --config 70_240 \
        --n-startup-trials 2 --n-iterations 1 --n-jobs 2
"""
from __future__ import annotations
import argparse
import pathlib
import shutil
import subprocess
import sys

AE      = pathlib.Path(__file__).resolve().parent.parent
ROOT    = AE.parent
AT_DIR  = ROOT / "experiments" / "autotuner_rsz"
AT_RUN  = AT_DIR / "run_autotuner.py"
SETUP   = AE / "design_setup.py"

STAGE_ORDER = ["setup", "base", "tune", "backend"]


def run(cmd, cwd=None) -> None:
    shown = " ".join(str(c) for c in cmd)
    print(f"\n[run_autotuner_exp] $ {shown}" + (f"   (cwd={cwd.name})" if cwd else ""), flush=True)
    rc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None).returncode
    if rc != 0:
        sys.exit(f"[run_autotuner_exp][ERROR] step failed (rc={rc}): {shown}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage + run the AutoTuner baseline on one config.")
    ap.add_argument("--pdk", required=True, choices=["asap7", "nangate45"])
    ap.add_argument("--design", required=True, help="aes | ibex | jpeg")
    ap.add_argument("--config", required=True, help="<util>_<cp>, e.g. 70_650")
    ap.add_argument("--stage", default="all",
                    choices=["all"] + STAGE_ORDER,
                    help="Run a single stage (default: all = setup->base->tune->backend)")
    ap.add_argument("--n-startup-trials", type=int, default=20)
    ap.add_argument("--n-iterations",     type=int, default=15)
    ap.add_argument("--n-jobs",           type=int, default=4)
    ap.add_argument("--resume", action="store_true", help="resume a previous Optuna study")
    ap.add_argument("--archive", action="store_true",
                    help="after the run, copy AT results into the dataset config folder "
                         "(per-config archive so sweep points don't overwrite each other)")
    args = ap.parse_args()

    stages = STAGE_ORDER if args.stage == "all" else [args.stage]

    if not AT_RUN.exists():
        sys.exit(f"[run_autotuner_exp][ERROR] AutoTuner entry not found: {AT_RUN}")

    at_common = [sys.executable, AT_RUN, "--design", args.design, "--pdk", args.pdk]

    if "setup" in stages:
        run([sys.executable, SETUP, "--pdk", args.pdk, "--design", args.design,
             "--config", args.config])

    if "base" in stages:
        run(at_common + ["--run-stage", "base"], cwd=AT_DIR)

    if "tune" in stages:
        cmd = at_common + ["--run-stage", "tune",
                           "--n-startup-trials", args.n_startup_trials,
                           "--n-iterations", args.n_iterations,
                           "--n-jobs", args.n_jobs]
        if args.resume:
            cmd.append("--resume")
        run(cmd, cwd=AT_DIR)

    if "backend" in stages:
        run(at_common + ["--run-stage", "backend"], cwd=AT_DIR)

    if args.archive:
        src = AT_DIR / "results" / args.pdk / args.design
        dst = (AE / "dataset" / args.pdk / args.design
               / f"{args.design}_{args.config}" / "results")
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"[run_autotuner_exp] archived results -> {dst.relative_to(ROOT)}")
        else:
            print(f"[run_autotuner_exp][WARN] no results to archive at {src}")

    print(f"\n[run_autotuner_exp] done: AT {args.pdk}/{args.design}/{args.config} "
          f"stage(s)={','.join(stages)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
