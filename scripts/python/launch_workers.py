#!/usr/bin/env python3
"""
launch_workers.py — parallel plan executor for the agentic timing-closure loop.

Runs one Docker/OpenROAD process per plan in parallel. All OpenROAD output goes
to per-plan log files. Classifies errors from logs and writes structured metrics.

Does NOT select or promote a best plan. That is the Selector agent's job.
After this script completes, invoke the Selector agent to evaluate results,
choose the best plan, and decide whether to continue or stop the loop.

Usage:
    # Run all plans for an iteration
    python3 Scripts/python/launch_workers.py \\
        --design aes --agent claude --iteration 10 --plan-count 4

    # Rerun specific plans only (Executor retry after TCL fix)
    python3 Scripts/python/launch_workers.py \\
        --design aes --agent claude --iteration 10 --plan-count 4 \\
        --plans plan2 plan4

Exit codes:
    0  — all targeted plans succeeded
    1  — one or more plans failed (check plan_metrics.csv per plan)
    2  — infrastructure error (seed ODB missing, plan dir missing, etc.)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE    = pathlib.Path(__file__).resolve().parent.parent.parent  # Scripts/python/ → Scripts/ → workspace
DOCKER_IMAGE = "openroad/flow-ubuntu22.04-builder:0b569c"
OPENROAD_BIN = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"

# Error patterns that indicate the generated TCL script is broken.
# These are Executor-fixable — Executor should read log, fix TCL, retry.
TCL_ERROR_PATTERNS = [
    r"undefined proc",
    r"wrong # args",
    r"invalid command name",
    r"can't read",
    r"no such variable",
    r"extra characters after close-quote",
    r"missing close-bracket",
    r"syntax error in expression",
    r"\[Error\] TCL",
    r"Error in startup script",
    r"wrong type",
    r"no value given for parameter",
    r"bad option",
    r"called with too",
]

# Error patterns that indicate OpenROAD hit a design-level failure.
# These are NOT Executor-fixable — report as failed result and move on.
OPENROAD_ERROR_PATTERNS = [
    r"\[ERROR",
    r"Placement failed",
    r"check_placement.*error",
    r"Segmentation fault",
    r"Aborted",
    r"assert.*failed",
    r"Cannot legalize",
]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def to_docker_path(host_path: pathlib.Path) -> str:
    """Convert a host absolute path under WORKSPACE to /workspace/... form."""
    try:
        rel = host_path.relative_to(WORKSPACE)
        return f"/workspace/{rel}"
    except ValueError:
        raise SystemExit(f"Path {host_path} is not under workspace {WORKSPACE}")


def seed_odb(agent: str, design: str, iteration: int) -> pathlib.Path:
    """Return the seed ODB path for this iteration.

    The Reporter (build_baseline_prompt.py) already copied the correct ODB
    into LLM_iterations/Iteration<N>/start/seed.odb before the Executor runs.
    """
    p = (WORKSPACE / "work_dir" / agent / design
         / "LLM_iterations" / f"Iteration{iteration}"
         / "start" / "seed.odb")
    if not p.exists():
        raise SystemExit(
            f"[INFRA] Seed ODB not found: {p}\n"
            f"  Run build_baseline_prompt.py --iteration {iteration} first "
            f"(it creates start/seed.odb)."
        )
    return p


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def classify_error(log_text: str, output_odb: pathlib.Path) -> Tuple[str, str]:
    """
    Returns (status, first_error_line).

    status values:
        "success"       — output.odb exists, no errors detected
        "tcl_error"     — TCL command error; Executor should fix script and retry
        "openroad_fail" — OpenROAD design-level failure; report as result, do not retry
        "infra_error"   — Docker/file infrastructure issue; investigate separately
    """
    if output_odb.exists():
        return "success", ""

    # Scan log for first matching error line
    for line in log_text.splitlines():
        for pattern in TCL_ERROR_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                return "tcl_error", line.strip()

    for line in log_text.splitlines():
        for pattern in OPENROAD_ERROR_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                return "openroad_fail", line.strip()

    return "infra_error", "output.odb missing and no recognisable error pattern found in log"


# ---------------------------------------------------------------------------
# Metrics parsing
# ---------------------------------------------------------------------------

def parse_timing(timing_file: pathlib.Path) -> Tuple[Optional[float], Optional[float]]:
    if not timing_file.exists():
        return None, None
    wns = tns = None
    for line in timing_file.read_text().splitlines():
        s = line.strip()
        if s.startswith("WNS:"):
            try:
                wns = float(s.split(":", 1)[1])
            except ValueError:
                pass
        elif s.startswith("TNS:"):
            try:
                tns = float(s.split(":", 1)[1])
            except ValueError:
                pass
    return wns, tns


def parse_area_util(area_file: pathlib.Path) -> Tuple[Optional[float], Optional[float]]:
    if not area_file.exists():
        return None, None
    for line in area_file.read_text().splitlines():
        m = re.search(r"Design area\s+([0-9.eE+-]+)\s+um\^2\s+([0-9.]+)%", line)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def parse_power(power_file: pathlib.Path) -> Optional[float]:
    if not power_file.exists():
        return None
    for line in power_file.read_text().splitlines():
        if line.startswith("Total"):
            fields = line.split()
            if len(fields) >= 5:
                try:
                    return float(fields[4])
                except ValueError:
                    pass
    return None


def parse_violations(viol_file: pathlib.Path) -> Optional[int]:
    if not viol_file.exists():
        return None
    try:
        return int(viol_file.read_text().splitlines()[0].strip())
    except (ValueError, IndexError):
        return None


def collect_metrics(plan_dir: pathlib.Path, stage_tag: str) -> Dict:
    wns, tns   = parse_timing(plan_dir / f"{stage_tag}_timing.txt")
    area, util = parse_area_util(plan_dir / f"{stage_tag}_area.rpt")
    power      = parse_power(plan_dir / f"{stage_tag}_power.rpt")
    violations = parse_violations(plan_dir / "violating_endpoint.txt")
    return {
        "wns":                  wns,
        "tns":                  tns,
        "area_um2":             area,
        "util_percent":         util,
        "power_mw":             power,
        "violating_endpoints":  violations,
    }


# ---------------------------------------------------------------------------
# Per-plan worker
# ---------------------------------------------------------------------------

def run_plan(
    plan_name: str,
    plan_dir: pathlib.Path,
    seed_odb_path: pathlib.Path,
    agent: str,
    design: str,
    iteration: int,
) -> Dict:
    """
    Execute one plan inside Docker. All output goes to plan_dir/<plan_name>.log.
    Returns a result dict suitable for plan_metrics.csv.
    """
    log_file = plan_dir / f"{plan_name}.log"
    tcl_file = plan_dir / "run_plan.tcl"
    input_odb  = plan_dir / "input.odb"
    output_odb = plan_dir / "output.odb"

    result = {
        "plan":                plan_name,
        "status":              "infra_error",
        "error_type":          "",
        "first_error":         "",
        "wns":                 "",
        "tns":                 "",
        "area_um2":            "",
        "util_percent":        "",
        "power_mw":            "",
        "violating_endpoints": "",
        "log":                 str(log_file),
    }

    # --- Pre-flight checks ---
    if not tcl_file.exists():
        result["first_error"] = f"run_plan.tcl not found in {plan_dir}"
        _write_log(log_file, f"[INFRA] {result['first_error']}\n")
        return result

    # --- Copy seed ODB ---
    try:
        shutil.copy2(seed_odb_path, input_odb)
    except OSError as exc:
        result["first_error"] = f"Failed to copy seed ODB: {exc}"
        _write_log(log_file, f"[INFRA] {result['first_error']}\n")
        return result

    # --- Build Docker command ---
    sdc_file  = WORKSPACE / "work_dir" / agent / design / "base" / "constraint.sdc"
    lef_dir   = WORKSPACE / "OpenROAD-flow-scripts" / "flow" / "platforms" / "asap7" / "lef"
    lib_dir   = WORKSPACE / "OpenROAD-flow-scripts" / "flow" / "platforms" / "asap7" / "lib" / "NLDM"
    setrc_tcl = WORKSPACE / "OpenROAD-flow-scripts" / "flow" / "platforms" / "asap7" / "setRC.tcl"

    env_vars = {
        "INPUT_DB":   to_docker_path(input_odb),
        "OUTPUT_DB":  to_docker_path(output_odb),
        "OUTPUT_DIR": to_docker_path(plan_dir) + "/",
        "SDC_FILE":   to_docker_path(sdc_file),
        "LEF_DIR":    to_docker_path(lef_dir) + "/",
        "LIB_DIR":    to_docker_path(lib_dir) + "/",
        "SETRC_TCL":  to_docker_path(setrc_tcl),
        "STAGE_TAG":  plan_name,
        "FLOW_HOME":  "/workspace/OpenROAD-flow-scripts/flow",
    }

    cmd = ["docker", "run", "--rm", "--user", "root"]
    for k, v in env_vars.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["-v", f"{WORKSPACE}:/workspace"]
    cmd += ["-w", "/workspace"]
    cmd += [DOCKER_IMAGE]
    cmd += [OPENROAD_BIN, "-no_init", "-exit", to_docker_path(tcl_file)]

    # --- Run Docker, all output → log file ---
    start = time.time()
    _write_log(log_file,
               f"=== {plan_name} — Iteration {iteration} ===\n"
               f"Command: {' '.join(cmd)}\n"
               f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
               f"{'=' * 60}\n\n")

    try:
        with open(log_file, "a") as lf:
            proc = subprocess.run(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
            )
        exit_code = proc.returncode
    except FileNotFoundError:
        result["first_error"] = "docker binary not found"
        _write_log(log_file, f"\n[INFRA] docker not found in PATH\n")
        return result
    except Exception as exc:
        result["first_error"] = f"subprocess error: {exc}"
        _write_log(log_file, f"\n[INFRA] Subprocess error: {exc}\n")
        return result

    elapsed = time.time() - start
    _write_log(log_file,
               f"\n{'=' * 60}\n"
               f"Exit code: {exit_code}  |  Elapsed: {elapsed:.1f}s\n")

    # --- Classify result ---
    log_text = log_file.read_text(errors="replace")
    status, first_error = classify_error(log_text, output_odb)

    result["status"]      = status
    result["first_error"] = first_error

    if status == "tcl_error":
        result["error_type"] = "tcl_error"
    elif status == "openroad_fail":
        result["error_type"] = "openroad_fail"
    elif status == "infra_error":
        result["error_type"] = "infra_error"
    else:
        # Parse metrics from artifact files
        metrics = collect_metrics(plan_dir, plan_name)
        result.update({k: ("" if v is None else v) for k, v in metrics.items()})

    # --- Write plan_metrics.csv ---
    _write_plan_metrics(plan_dir, result)

    return result


def _write_log(log_file: pathlib.Path, text: str) -> None:
    """Append text to log file (creates if missing)."""
    with open(log_file, "a") as f:
        f.write(text)


def _write_plan_metrics(plan_dir: pathlib.Path, result: Dict) -> None:
    """Write plan_metrics.csv for this plan."""
    fields = [
        "plan", "status", "error_type", "first_error",
        "wns", "tns", "area_um2", "util_percent",
        "power_mw", "violating_endpoints", "log",
    ]
    csv_path = plan_dir / "plan_metrics.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerow(result)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Parallel plan executor.")
    ap.add_argument("--design",     required=True)
    ap.add_argument("--agent",      required=True)
    ap.add_argument("--iteration",  required=True, type=int)
    ap.add_argument("--plan-count", required=True, type=int,
                    help="Total number of plans in this iteration")
    ap.add_argument("--plans",      nargs="+", metavar="planN",
                    help="Run only these plans (e.g. --plans plan2 plan4). "
                         "Default: run all plans.")
    return ap.parse_args()


def main() -> int:
    args    = parse_args()
    design  = args.design
    agent   = args.agent
    iterN   = args.iteration

    iter_dir = (WORKSPACE / "work_dir" / agent / design
                / "LLM_iterations" / f"Iteration{iterN}")

    # Determine which plans to run
    all_plans  = [f"plan{i}" for i in range(1, args.plan_count + 1)]
    target_plans = args.plans if args.plans else all_plans

    # Validate requested plans exist as directories
    missing = [p for p in target_plans if not (iter_dir / p).is_dir()]
    if missing:
        print(f"[ERROR] Plan directories not found: {missing}", file=sys.stderr)
        return 2

    # Get seed ODB (raises SystemExit with message if missing)
    try:
        seed = seed_odb(agent, design, iterN)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"Iteration {iterN} | design={design} | agent={agent} | "
          f"plans={target_plans} | seed={seed.name}")
    print(f"Logs: {iter_dir}/<plan>/<plan>.log")
    print()

    # Run plans in parallel
    results: List[Dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(target_plans)) as pool:
        futures = {
            pool.submit(
                run_plan,
                plan_name=p,
                plan_dir=iter_dir / p,
                seed_odb_path=seed,
                agent=agent,
                design=design,
                iteration=iterN,
            ): p
            for p in target_plans
        }
        for future in concurrent.futures.as_completed(futures):
            plan_name = futures[future]
            try:
                res = future.result()
            except Exception as exc:
                res = {
                    "plan": plan_name, "status": "infra_error",
                    "error_type": "infra_error",
                    "first_error": str(exc),
                    "wns": "", "tns": "", "area_um2": "",
                    "util_percent": "", "power_mw": "",
                    "violating_endpoints": "",
                    "log": str(iter_dir / plan_name / f"{plan_name}.log"),
                }
            results.append(res)
            _print_result_line(res)

    # Sort results by plan name for deterministic output
    results.sort(key=lambda r: r["plan"])

    # Write iteration summary CSV
    _write_iteration_summary(iter_dir, results)

    print(f"\nIteration summary written to: {iter_dir}/iteration_summary.csv")
    print("Invoke the Selector agent to evaluate results and promote the best plan.")

    # Return non-zero if any targeted plan failed
    any_failed = any(r["status"] != "success" for r in results)
    return 1 if any_failed else 0


def _print_result_line(result: Dict) -> None:
    """Print a single status line per plan. This is the only screen output."""
    p = result["plan"]
    s = result["status"]
    if s == "success":
        print(f"  [{p}] success | WNS={result['wns']} ps | TNS={result['tns']} ps | "
              f"area={result['area_um2']} um² | util={result['util_percent']}% | "
              f"violations={result['violating_endpoints']}")
    else:
        print(f"  [{p}] {s} | {result['first_error'][:80]} | log: {result['log']}")


def _write_iteration_summary(iter_dir: pathlib.Path, results: List[Dict]) -> None:
    """Write iteration_summary.csv aggregating all plan results."""
    fields = [
        "plan", "status", "error_type", "first_error",
        "wns", "tns", "area_um2", "util_percent",
        "power_mw", "violating_endpoints", "log",
    ]
    csv_path = iter_dir / "iteration_summary.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)



if __name__ == "__main__":
    sys.exit(main())
