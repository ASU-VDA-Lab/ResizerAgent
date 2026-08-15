#!/usr/bin/env python3
"""
experiments/autotuner_rsz/run_autotuner.py — Standalone entry point for AutoTuner.

Completely standalone — never touches run.py, work_dir/, or any agentic
flow infrastructure. Copy experiments/autotuner_rsz/ to another server and it runs.

Stages:
  base  — Run make cts inside Docker (openroad-flow-scripts/), copy
           4_1_cts.odb + SDC to experiments/autotuner_rsz/results/.../base/, generate
           placement-parasitic artifacts via generate_base_artifacts.tcl.

  tune  — Run Optuna TPE hyperparameter search starting from the base_cts.odb
           produced by --run-stage base. No LLM involved.

Prerequisites:
  pip install optuna

Usage:
  # Step 1 — generate base_cts.odb (must run first)
  python3 experiments/autotuner_rsz/run_autotuner.py \\
      --design aes --pdk asap7 --run-stage base

  # Step 2 — run autotuner search
  python3 experiments/autotuner_rsz/run_autotuner.py \\
      --design aes --pdk asap7 --run-stage tune \\
      --n-startup-trials 20 --n-iterations 15 --n-jobs 4

  # Resume a previous study
  python3 experiments/autotuner_rsz/run_autotuner.py \\
      --design aes --pdk asap7 --run-stage tune --resume

Output (experiments/autotuner_rsz/results/<pdk>/<design>/):
  base/
    base_cts.odb            — raw post-CTS database (seed for all trials)
    constraint.sdc          — post-CTS SDC
    base_timing.txt         — WNS/TNS at placement parasitics
    base_metrics.csv        — area, power, util
    base_placement_report.txt
  autotuner_results.csv     — all trials: params + WNS/TNS/area/power/score
  autotuner_best.json       — best parameter set
  autotuner_comparison.csv  — baseline vs best
  experiment_summary.csv    — per-iteration best summary
  iteration_metrics.csv     — all per-trial metrics per iteration
  params_history.csv        — raw Optuna parameter suggestions
  runtimes.csv              — timing: base_cts, base_artifacts, startup, iter_N, total
  optuna_study.db           — Optuna SQLite (enables --resume)
  trial_NNN/                — per-trial artifacts
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

os.environ.setdefault("DOCKER_API_VERSION", "1.43")

# ---------------------------------------------------------------------------
# Standalone layout — everything relative to experiments/autotuner_rsz/
# ---------------------------------------------------------------------------

AUTOTUNER_ROOT = pathlib.Path(__file__).resolve().parent        # experiments/autotuner_rsz/
WORKSPACE      = AUTOTUNER_ROOT.parent.parent                   # 2A-new-context/
SCRIPTS_PY     = AUTOTUNER_ROOT / "scripts" / "python"         # local, standalone

if str(SCRIPTS_PY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_PY))

from pdk_configs import get as _get_pdk   # noqa: E402


# ---------------------------------------------------------------------------
# ORFS config
# ---------------------------------------------------------------------------

class _OrfsConfig:
    def __init__(self, root_dir_name, docker_image, cts_make_target,
                 cts_odb_name, cts_sdc_name):
        self.root_dir_name    = root_dir_name
        self.docker_image     = docker_image
        self.cts_make_target  = cts_make_target
        self.cts_odb_name     = cts_odb_name
        self.cts_sdc_name     = cts_sdc_name


_ORFS_CFG = _OrfsConfig("openroad-flow-scripts",
                        "orfs_ra:latest",
                        "cts", "4_1_cts.odb", "4_cts.sdc")

DOCKER_OPENROAD = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _HelpfulParser(argparse.ArgumentParser):
    """On an argument error, show the full help (usage + options + examples)
    followed by the error message."""
    def error(self, message):
        self.print_help(sys.stderr)
        self.exit(2, f"\n{self.prog}: error: {message}\n")


def parse_args() -> argparse.Namespace:
    ap = _HelpfulParser(
        description="AutoTuner: standalone CTS + Optuna repair_timing search.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--design",    required=True,
                    help="Design name (e.g. gcd, aes, ibex)")
    ap.add_argument("--pdk",       required=True, choices=["asap7", "nangate45"],
                    help="Target PDK")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-stage", choices=["base", "tune", "backend"],
                       dest="run_stage",
                       help="base: run make cts + artifact generation; "
                            "tune: run Optuna TPE search (requires base); "
                            "backend: run make route finish on best ODB (requires tune)")
    group.add_argument("--clean", choices=["base", "tune", "backend", "all"],
                       dest="clean",
                       help="base: delete base/ artifacts; "
                            "tune: delete tune artifacts (keeps base/); "
                            "backend: delete backend artifacts; "
                            "all: delete everything for this design")
    # tune-stage args
    ap.add_argument("--n-startup-trials", type=int, default=20,
                    dest="n_startup_trials",
                    help="[tune] Sequential random trials before TPE (default: 20)")
    ap.add_argument("--n-iterations", type=int, default=15,
                    dest="n_iterations",
                    help="[tune] TPE-guided parallel iterations (default: 15)")
    ap.add_argument("--n-jobs",    type=int, default=4, dest="n_jobs",
                    help="[tune] Parallel trials per iteration (default: 4)")
    ap.add_argument("--resume",    action="store_true",
                    help="[tune] Resume existing Optuna study from optuna_study.db")
    ap.add_argument("--docker-image", default=_ORFS_CFG.docker_image, dest="docker_image",
                    help="Docker image (name[:tag]) to run in (default: %(default)s)")
    ap.add_argument("--finalize",  action="store_true",
                    help="[tune] Skip Phase 1/2, write summary files from existing study "
                         "(for recovering from a crash after trials completed)")
    return ap.parse_args()


def _clean(design: str, pdk: str, orfs_cfg, what: str) -> int:
    import shutil
    output_dir = AUTOTUNER_ROOT / "results" / pdk / design
    log_dir    = AUTOTUNER_ROOT / "logs"    / pdk / design
    orfs_flow_dir    = WORKSPACE / orfs_cfg.root_dir_name / "flow"
    orfs_results_dir = orfs_flow_dir / "results" / pdk / design / "base"

    _tune_files = [
        "autotuner_results.csv", "autotuner_best.json", "autotuner_config.json",
        "experiment_summary.csv", "iteration_metrics.csv", "params_history.csv",
        "runtimes.csv", "optuna_study.db",
        "best_output.odb", "best_run_plan.tcl", "best_run.log",
        "autotuner_comparison.csv",
    ]
    _backend_files = [
        "autotuner_comparison.csv",
    ]
    _backend_logs = ["backend_route_finish.log"]

    def _rm(p: pathlib.Path):
        if p.is_dir():
            shutil.rmtree(p)
            print(f"  removed dir : {p}")
        elif p.exists():
            p.unlink()
            print(f"  removed file: {p}")

    def _rm_docker(p: pathlib.Path):
        """Remove a path that may be root-owned (created by Docker)."""
        if not p.exists():
            return
        docker_path = "/workspace/" + p.resolve().relative_to(WORKSPACE).as_posix()
        rc = subprocess.run(
            ["docker", "run", "--rm", "--user", "root",
             "-v", f"{WORKSPACE}:/workspace",
             orfs_cfg.docker_image,
             "rm", "-rf", docker_path],
            capture_output=True,
        ).returncode
        if rc == 0:
            print(f"  removed dir : {p}")
        else:
            print(f"  WARNING: could not remove {p} (exit {rc})")

    def _rm_glob(directory: pathlib.Path, pattern: str):
        for p in sorted(directory.glob(pattern)):
            _rm(p)

    print(f"[clean] design={design}  pdk={pdk}  what={what}")

    if what in ("base", "all"):
        _rm(output_dir / "base")
        for f in ["base_cts.log", "base_artifacts.log"]:
            _rm(log_dir / f)
        # Also wipe the openroad-flow-scripts flow results for this design so make cts rebuilds from scratch
        _rm_docker(orfs_flow_dir / "results" / pdk / design)

    if what in ("tune", "all"):
        for f in _tune_files:
            _rm(output_dir / f)
        _rm_glob(output_dir, "trial_*/")
        _rm(output_dir / "startup_trials")
        _rm(output_dir / "main_trials")
        _rm_glob(output_dir, "_tmp_*/")
        for f in ["tune_resume.log"]:
            _rm(log_dir / f)

    if what in ("backend", "all"):
        for f in _backend_files:
            _rm(output_dir / f)
        for f in _backend_logs:
            _rm(log_dir / f)
        # Clean ORFS route/finish results (5_* and 6_*) — root-owned, use Docker
        if orfs_results_dir.exists():
            docker_base = "/workspace/" + orfs_results_dir.resolve().relative_to(WORKSPACE).as_posix()
            cmd_str = f"rm -rf {docker_base}/5_* {docker_base}/6_*"
            rc = subprocess.run(
                ["docker", "run", "--rm", "--user", "root",
                 "-v", f"{WORKSPACE}:/workspace",
                 orfs_cfg.docker_image, "bash", "-c", cmd_str],
                capture_output=True,
            ).returncode
            if rc == 0:
                print(f"  removed 5_*/6_* in {orfs_results_dir}")
            else:
                print(f"  WARNING: could not remove 5_*/6_* in {orfs_results_dir} (exit {rc})")

    if what == "all":
        # Remove the design output dir entirely if now empty
        try:
            output_dir.rmdir()
            print(f"  removed dir : {output_dir}")
        except OSError:
            pass

    print(f"[clean] Done.")
    return 0


def main() -> int:
    args     = parse_args()
    orfs_cfg = _ORFS_CFG
    orfs_cfg.docker_image = args.docker_image
    pdk_cfg  = _get_pdk(args.pdk)

    if args.clean:
        return _clean(args.design, args.pdk, orfs_cfg, args.clean)

    print(f"[autotuner] ORFS         : openroad-flow-scripts (submodule)")
    print(f"[autotuner] Docker image : {orfs_cfg.docker_image}")
    print(f"[autotuner] Stage        : {args.run_stage}")

    if args.run_stage == "base":
        from base_stage import run_base_stage  # noqa: E402
        run_base_stage(
            design         = args.design,
            pdk            = args.pdk,
            orfs_cfg       = orfs_cfg,
            pdk_cfg        = pdk_cfg,
            workspace      = WORKSPACE,
            autotuner_root = AUTOTUNER_ROOT,
        )

    elif args.run_stage == "tune":
        from autotuner import run_autotuner  # noqa: E402
        docker_fh = f"/workspace/{orfs_cfg.root_dir_name}/flow"
        run_autotuner(
            design           = args.design,
            pdk              = args.pdk,
            n_startup_trials = args.n_startup_trials,
            n_iterations     = args.n_iterations,
            n_jobs           = args.n_jobs,
            resume           = args.resume,
            workspace        = WORKSPACE,
            autotuner_root   = AUTOTUNER_ROOT,
            docker_image     = orfs_cfg.docker_image,
            docker_openroad  = DOCKER_OPENROAD,
            docker_flow_home = docker_fh,
            pdk_cfg          = pdk_cfg,
            orfs_cfg         = orfs_cfg,
            finalize         = args.finalize,
        )

    elif args.run_stage == "backend":
        from backend_stage import run_backend_stage  # noqa: E402
        run_backend_stage(
            design         = args.design,
            pdk            = args.pdk,
            orfs_cfg       = orfs_cfg,
            pdk_cfg        = pdk_cfg,
            workspace      = WORKSPACE,
            autotuner_root = AUTOTUNER_ROOT,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
