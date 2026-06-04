"""
experiments/autotuner_rsz/autotuner.py — Core AutoTuner logic for repair_timing hyperparameter search.

Replaces the LLM Planner with an Optuna TPE search over:
  - sequence: ordered subset of 9 repair_timing moves (include + weight encoding)
  - repair_tns, max_passes, max_iterations, setup_margin: numeric knobs

Each trial starts from the same base_cts.odb. All trials are independent —
no ODB state is carried forward between trials (unlike the agentic loop).

Objective: minimize effective clock period = clock_period_ps - WNS_ps.
"""
from __future__ import annotations

import csv
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MOVES_ALL: List[str] = [
    "sizeup", "sizedown", "vt_swap", "buffer", "split",
    "unbuffer", "clone", "sizeup_match", "swap",
]


def get_valid_moves(pdk_cfg) -> List[str]:
    """Return move list for the given PDK. vt_swap excluded when PDK has no VT tiers."""
    if not pdk_cfg.vt_tiers:
        return [m for m in MOVES_ALL if m != "vt_swap"]
    return list(MOVES_ALL)

ERROR_METRIC = -9e99   # worst value for maximize direction
_RESULTS_LOCK = threading.Lock()

_RESULTS_HEADER = [
    "trial_idx", "sequence", "repair_tns", "max_passes", "max_iterations",
    "setup_margin", "wns_ps", "tns_ps", "area_um2", "util_pct", "power_W",
    "objective", "status", "runtime_s",
]

_RUNTIMES_HEADER = ["stage", "runtime_s", "start_time"]

_ITER_METRICS_HEADER = [
    "iteration", "trial_idx", "sequence",
    "repair_tns", "max_passes", "max_iterations", "setup_margin",
    "wns_ps", "tns_ps", "area_um2", "util_pct", "power_W",
    "score", "status", "runtime_s",
]

_SUMMARY_HEADER = [
    "iteration", "best_trial_idx", "best_sequence",
    "best_score", "best_wns_ps", "best_tns_ps",
    "suggestion_time_s", "max_trial_time_s", "iter_total_runtime_s",
    "cumulative_best_score",
]


# ---------------------------------------------------------------------------
# Parameter decoding
# ---------------------------------------------------------------------------

def decode_params(trial: optuna.Trial, valid_moves: List[str]) -> Tuple[List[str], Dict]:
    """
    Suggest parameters from Optuna trial, return (sequence, run_knobs).

    Sequence encoding (dual-weight system):
      - include_<move> ∈ {0, 1}: whether move is in the sequence
      - weight_<move> ∈ [1.0, 10.0]: ordering weight (conditional on include=1)
        higher weight → earlier position in sequence
      - Guard: if all includes=0, use first valid move as fallback

    Numeric knobs: repair_tns, max_passes (≤ max_iterations), max_iterations, setup_margin
    """
    include: Dict[str, int]   = {}
    weights: Dict[str, float] = {}

    for move in valid_moves:
        include[move] = trial.suggest_int(f"include_{move}", 0, 1)
        if include[move] == 1:
            weights[move] = trial.suggest_float(f"weight_{move}", 1.0, 10.0)

    selected = [(m, weights[m]) for m in valid_moves if include[m] == 1]
    if not selected:
        selected = [(valid_moves[0], 1.0)]  # guard: always at least 1 move

    selected.sort(key=lambda x: x[1], reverse=True)  # higher weight → earlier position
    sequence = [m for m, _ in selected]

    max_iterations = trial.suggest_int("max_iterations", 1, 50000)
    knobs = {
        "repair_tns":     trial.suggest_float("repair_tns",   1.0, 100.0),
        "max_passes":     trial.suggest_int(  "max_passes",   1,   min(10000, max_iterations)),
        "max_iterations": max_iterations,
        "setup_margin":   trial.suggest_float("setup_margin", 0.0, 200.0),
    }
    return sequence, knobs


# ---------------------------------------------------------------------------
# Metric parsing (mirrors run.py helpers exactly)
# ---------------------------------------------------------------------------

def _parse_timing_file(p: pathlib.Path) -> Tuple[Optional[float], Optional[float]]:
    if not p.exists():
        return None, None
    wns = tns = None
    for line in p.read_text().splitlines():
        s = line.strip()
        if s.startswith("WNS:"):
            try: wns = float(s.split(":", 1)[1])
            except ValueError: pass
        elif s.startswith("TNS:"):
            try: tns = float(s.split(":", 1)[1])
            except ValueError: pass
    return wns, tns


def _parse_area_file(p: pathlib.Path) -> Tuple[Optional[float], Optional[float]]:
    if not p.exists():
        return None, None
    for line in p.read_text().splitlines():
        m = re.search(r"Design area\s+([0-9.eE+-]+)\s+um\^2\s+([0-9.]+)%", line)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def _parse_power_file(p: pathlib.Path) -> Optional[float]:
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith("Total"):
            fields = line.split()
            if len(fields) >= 5:
                try: return float(fields[4])
                except ValueError: pass
    return None


def _collect_metrics(trial_dir: pathlib.Path, stage_tag: str,
                     time_unit_ps: float) -> Dict:
    wns, tns   = _parse_timing_file(trial_dir / f"{stage_tag}_timing.txt")
    area, util = _parse_area_file(trial_dir / f"{stage_tag}_area.rpt")
    power      = _parse_power_file(trial_dir / f"{stage_tag}_power.rpt")
    if wns  is not None: wns  *= time_unit_ps
    if tns  is not None: tns  *= time_unit_ps
    return {"wns": wns, "tns": tns, "area_um2": area, "util_pct": util, "power_W": power}


def _parse_clock_period(sdc_path: pathlib.Path, time_unit_ps: float) -> float:
    """
    Extract clock period in ps from a write_sdc-generated constraint.sdc.
    SDC contains 'create_clock ... -period <val>' in PDK native time units.
    Multiply by time_unit_ps to get ps (e.g. nangate45: 0.41 ns × 1000 = 410 ps).
    """
    sdc_text = sdc_path.read_text()
    m = re.search(r"create_clock\s+.*?-period\s+([0-9.]+)", sdc_text)
    if not m:
        raise ValueError(f"create_clock -period not found in {sdc_path}")
    return float(m.group(1)) * time_unit_ps


def _classify_error(log_text: str, timing_file: pathlib.Path) -> Tuple[str, str]:
    TCL_PATTERNS = [
        r"undefined proc", r"wrong # args", r"invalid command name",
        r"can't read", r"no such variable", r"extra characters after close-quote",
        r"missing close-bracket", r"syntax error in expression",
        r"\[Error\] TCL", r"Error in startup script",
    ]
    OR_PATTERNS = [
        r"\[ERROR", r"Placement failed", r"Segmentation fault",
        r"Aborted", r"Cannot legalize",
    ]
    if timing_file.exists():
        return "success", ""
    for line in log_text.splitlines():
        for pat in TCL_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return "tcl_error", line.strip()
    for line in log_text.splitlines():
        for pat in OR_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return "openroad_fail", line.strip()
    return "infra_error", "timing file missing and no recognisable error pattern"


def _compute_objective(wns: float, tns: float, power: float,
                       baseline: Dict) -> float:
    """
    Weighted normalized improvement objective. Weights: WNS=0.4, TNS=0.4, power=0.2.

    score = 0.4 * (baseline_wns   - wns)   / baseline_wns
          + 0.4 * (baseline_tns   - tns)   / baseline_tns
          + 0.2 * (baseline_power - power) / baseline_power

    Each term is positive when current metric is better than baseline.
    WNS and TNS are negative (violations), so dividing by a negative baseline
    correctly signs the improvement fraction.
    Falls back gracefully if any baseline metric is missing.
    """
    b_wns   = baseline.get("wns")
    b_tns   = baseline.get("tns")
    b_power = baseline.get("power_W")

    has_wns   = wns   is not None and b_wns   and b_wns   != 0
    has_tns   = tns   is not None and b_tns   and b_tns   != 0
    has_power = power is not None and b_power and b_power != 0

    if not has_wns:
        return ERROR_METRIC

    score  = 0.4 * (b_wns - wns) / b_wns
    score += 0.4 * (b_tns - tns) / b_tns   if has_tns   else 0.0
    score += 0.2 * (b_power - power) / b_power if has_power else 0.0

    return score   # Optuna maximizes: higher score = more improvement over baseline


# ---------------------------------------------------------------------------
# Result I/O
# ---------------------------------------------------------------------------

def _append_result_row(results_csv: pathlib.Path, row: Dict) -> None:
    with _RESULTS_LOCK:
        new_file = not results_csv.exists()
        with open(results_csv, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_RESULTS_HEADER, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)


def _write_comparison_csv(
    output_dir: pathlib.Path,
    baseline: Dict,
    best: Dict,
    best_trial_tag: str,
) -> None:
    """Write autotuner_comparison.csv — same 3-row schema as final_comparison.csv."""
    path = output_dir / "autotuner_comparison.csv"

    def _f(v, fmt):
        return fmt.format(v) if isinstance(v, (int, float)) else ""

    base_row = ["default_baseline",
                _f(baseline.get("wns"),      "{:.3f}"),
                _f(baseline.get("tns"),      "{:.3f}"),
                _f(baseline.get("area_um2"), "{:.2f}"),
                _f(baseline.get("util_pct"), "{:.2f}"),
                _f(baseline.get("power_W"),  "{:.6e}")]
    best_row = [best_trial_tag,
                _f(best.get("wns"),      "{:.3f}"),
                _f(best.get("tns"),      "{:.3f}"),
                _f(best.get("area_um2"), "{:.2f}"),
                _f(best.get("util_pct"), "{:.2f}"),
                _f(best.get("power_W"),  "{:.6e}")]
    try:
        delta_row = [f"delta_{best_trial_tag}_minus_default",
                     "{:+.3f}".format(best["wns"]      - baseline["wns"]),
                     "{:+.3f}".format(best["tns"]      - baseline["tns"]),
                     "{:+.2f}".format(best["area_um2"] - baseline["area_um2"]),
                     "{:+.2f}".format(best["util_pct"] - baseline["util_pct"]),
                     "{:+.6e}".format(best["power_W"]  - baseline["power_W"])]
    except (TypeError, KeyError):
        delta_row = [f"delta_{best_trial_tag}_minus_default",
                     "N/A", "N/A", "N/A", "N/A", "N/A"]

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["flow", "wns_ps", "tns_ps", "area_um2", "util_pct", "power_W"])
        w.writerow(base_row)
        w.writerow(best_row)
        w.writerow(delta_row)


# ---------------------------------------------------------------------------
# Timing and tracking helpers
# ---------------------------------------------------------------------------

def _append_csv_row(csv_path: pathlib.Path, header: List[str], row: Dict) -> None:
    with _RESULTS_LOCK:
        new_file = not csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)


def _write_runtime_row(runtimes_csv: pathlib.Path, stage: str,
                       runtime_s: float, start_time: str) -> None:
    _append_csv_row(runtimes_csv, _RUNTIMES_HEADER, {
        "stage":      stage,
        "runtime_s":  f"{runtime_s:.2f}",
        "start_time": start_time,
    })


def _write_iter_metrics(iter_csv: pathlib.Path, iteration: int,
                        batch_trials: List, study) -> None:
    for t in batch_trials:
        ua  = t.user_attrs
        # study.ask() returns Trial; value only on FrozenTrial after study.tell()
        ft  = study.trials[t.number]
        val = ft.value
        _append_csv_row(iter_csv, _ITER_METRICS_HEADER, {
            "iteration":      iteration,
            "trial_idx":      t.number,
            "sequence":       ua.get("sequence", ""),
            "repair_tns":     f"{ua.get('repair_tns', ''):.2f}" if ua.get("repair_tns") else "",
            "max_passes":     ua.get("max_passes", ""),
            "max_iterations": ua.get("max_iterations", ""),
            "setup_margin":   f"{ua.get('setup_margin', ''):.2f}" if ua.get("setup_margin") else "",
            "wns_ps":         f"{ua.get('wns_ps', ''):.3f}" if ua.get("wns_ps") is not None else "",
            "tns_ps":         f"{ua.get('tns_ps', ''):.3f}" if ua.get("tns_ps") is not None else "",
            "area_um2":       f"{ua.get('area_um2', ''):.2f}" if ua.get("area_um2") else "",
            "util_pct":       f"{ua.get('util_pct', ''):.2f}" if ua.get("util_pct") else "",
            "power_W":        f"{ua.get('power_W', ''):.6e}" if ua.get("power_W") else "",
            "score":          f"{val:.4f}" if val is not None and val > ERROR_METRIC else "FAILED",
            "status":         ua.get("status", ""),
            "runtime_s":      ua.get("runtime_s", ""),
        })


def _write_summary_row(summary_csv: pathlib.Path, iteration: int,
                       batch_trials: List, study,
                       suggestion_time_s: float, max_trial_time_s: float,
                       iter_total_s: float) -> None:
    # Fetch FrozenTrials — Trial objects from study.ask() don't carry .value
    frozen    = [study.trials[t.number] for t in batch_trials]
    completed = [ft for ft in frozen
                 if ft.value is not None and ft.value > ERROR_METRIC]
    if not completed:
        return
    best = max(completed, key=lambda ft: ft.value)
    ua   = best.user_attrs
    try:
        cum_best = study.best_value
    except Exception:
        cum_best = best.value

    _append_csv_row(summary_csv, _SUMMARY_HEADER, {
        "iteration":            iteration,
        "best_trial_idx":       best.number,
        "best_sequence":        ua.get("sequence", ""),
        "best_score":           f"{best.value:.4f}",
        "best_wns_ps":          f"{ua.get('wns_ps', ''):.3f}" if ua.get("wns_ps") is not None else "",
        "best_tns_ps":          f"{ua.get('tns_ps', ''):.3f}" if ua.get("tns_ps") is not None else "",
        "suggestion_time_s":    f"{suggestion_time_s:.2f}",
        "max_trial_time_s":     f"{max_trial_time_s:.2f}",
        "iter_total_runtime_s": f"{iter_total_s:.2f}",
        "cumulative_best_score":f"{cum_best:.4f}",
    })


def _write_params_row(params_csv: pathlib.Path, trial,
                      iteration: str, valid_moves: List[str]) -> None:
    """Write raw Optuna parameter values — what the autotuner actually suggested."""
    params = trial.params
    row: Dict = {"trial_idx": trial.number, "iteration": iteration}
    for move in MOVES_ALL:
        if move in valid_moves:
            row[f"include_{move}"] = params.get(f"include_{move}", "")
            row[f"weight_{move}"]  = (f"{params[f'weight_{move}']:.3f}"
                                       if f"weight_{move}" in params else "")
        else:
            row[f"include_{move}"] = "N/A"
            row[f"weight_{move}"]  = "N/A"
    row["repair_tns"]     = f"{params.get('repair_tns', ''):.2f}" if "repair_tns" in params else ""
    row["max_passes"]     = params.get("max_passes", "")
    row["max_iterations"] = params.get("max_iterations", "")
    row["setup_margin"]   = f"{params.get('setup_margin', ''):.2f}" if "setup_margin" in params else ""
    row["sequence"]       = trial.user_attrs.get("sequence", "")

    # Build header dynamically on first call
    header = (["trial_idx", "iteration"] +
              [f"{pfx}_{m}" for m in MOVES_ALL for pfx in ("include", "weight")] +
              ["repair_tns", "max_passes", "max_iterations", "setup_margin", "sequence"])
    _append_csv_row(params_csv, header, row)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_autotuner(
    design:            str,
    pdk:               str,
    n_startup_trials:  int,
    n_iterations:      int,
    n_jobs:            int,
    resume:            bool,
    workspace:         pathlib.Path,
    autotuner_root:    pathlib.Path,
    docker_image:      str,
    docker_openroad:   str,
    docker_flow_home:  str,
    pdk_cfg,
    orfs_cfg,
    finalize:          bool = False,
) -> None:
    """
    Run Optuna TPE search over repair_timing sequence + knobs from base_cts.odb.

    workspace      — project root (2A-new-context/), needed for Docker path mapping
    autotuner_root — experiments/autotuner_rsz/, all results written here
    base_cts.odb   — expected at autotuner_root/results/orfs_<orfs>/<pdk>/<design>/base/
                     generated by run_autotuner.py --run-stage base
    finalize       — skip Phase 1/2 and write summary files from existing study
    """
    if finalize:
        resume = True  # finalize always needs the existing study
    # --- Paths ---
    # All paths are derived from autotuner_root — no dependency on run.py or work_dir/.
    output_dir  = autotuner_root / "results" / f"orfs_{orfs_cfg.name}" / pdk / design
    base_dir    = output_dir / "base"
    default_dir = output_dir / "default"
    output_dir.mkdir(parents=True, exist_ok=True)

    base_cts_odb     = base_dir / "base_cts.odb"
    sdc_file         = base_dir / "constraint.sdc"
    results_csv      = output_dir / "autotuner_results.csv"
    runtimes_csv     = output_dir / "runtimes.csv"
    iter_metrics_csv = output_dir / "iteration_metrics.csv"
    summary_csv      = output_dir / "experiment_summary.csv"
    params_csv       = output_dir / "params_history.csv"

    if not base_cts_odb.exists():
        raise FileNotFoundError(
            f"base_cts.odb not found: {base_cts_odb}\n"
            "Run 'python3 experiments/autotuner_rsz/run_autotuner.py --run-stage base' first."
        )
    if not sdc_file.exists():
        raise FileNotFoundError(f"constraint.sdc not found: {sdc_file}")

    time_unit_ps    = pdk_cfg.time_unit_ps
    clock_period_ps = _parse_clock_period(sdc_file, time_unit_ps)
    valid_moves     = get_valid_moves(pdk_cfg)

    # --- Docker path helper (mirrors run.py:to_docker) ---
    def _to_docker(host_path: pathlib.Path) -> str:
        return "/workspace/" + host_path.resolve().relative_to(workspace).as_posix()

    # --- PDK-specific paths ---
    orfs_flow_dir = workspace / orfs_cfg.root_dir_name / "flow"
    orfs_pdk_dir  = orfs_flow_dir / "platforms" / pdk
    lef_dir       = orfs_pdk_dir / "lef"
    lib_dir       = (orfs_pdk_dir / "lib" / pdk_cfg.lib_subdir
                     if pdk_cfg.lib_subdir else orfs_pdk_dir / "lib")
    setrc_tcl     = orfs_pdk_dir / pdk_cfg.setrc_tcl
    pdk_preamble  = autotuner_root / pdk_cfg.preamble_tcl

    # --- Baseline metrics: prefer default/ (post-GR), fall back to base/ (placement) ---
    baseline_metrics = _collect_metrics(default_dir, "default", time_unit_ps)
    baseline_source  = "default"
    if baseline_metrics["wns"] is None:
        baseline_metrics = _collect_metrics(base_dir, "base", time_unit_ps)
        baseline_source  = "base"
    has_baseline = baseline_metrics["wns"] is not None

    # --- Config snapshot ---
    (output_dir / "autotuner_config.json").write_text(json.dumps({
        "design": design, "pdk": pdk,
        "n_startup_trials": n_startup_trials, "n_iterations": n_iterations,
        "n_jobs": n_jobs, "resume": resume,
        "total_trials": n_startup_trials + n_iterations * n_jobs,
        "clock_period_ps": clock_period_ps,
        "valid_moves": valid_moves,
        "vt_swap_excluded": "vt_swap" not in valid_moves,
        "objective": "minimize effective_cp = clock_period_ps - WNS_ps",
        "search_space": {
            "include_<move>":  {"type": "int",   "range": [0, 1],
                                "moves": valid_moves},
            "weight_<move>":   {"type": "float", "range": [1.0, 10.0],
                                "note": "conditional on include==1"},
            "repair_tns":      {"type": "float", "range": [1.0, 100.0]},
            "max_iterations":  {"type": "int",   "range": [1, 50000]},
            "max_passes":      {"type": "int",   "range": [1, "min(10000, max_iterations)"],
                                "note": "upper bound = min(10000, sampled max_iterations)"},
            "setup_margin":    {"type": "float", "range": [0.0, 200.0]},
        },
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))

    print(f"\n{'='*70}")
    print(f"[autotuner] Design={design}  PDK={pdk}  Clock={clock_period_ps:.1f}ps")
    print(f"[autotuner] Startup={n_startup_trials}  Iterations={n_iterations}  "
          f"Width={n_jobs}  Total={n_startup_trials + n_iterations * n_jobs}  Resume={resume}")
    print(f"[autotuner] Moves ({len(valid_moves)}): {', '.join(valid_moves)}")
    if "vt_swap" not in valid_moves:
        print(f"[autotuner] vt_swap excluded (PDK has no VT tiers)")
    print(f"[autotuner] Docker image: {docker_image}")
    print(f"[autotuner] Output: {output_dir}")
    if has_baseline:
        print(f"[autotuner] Baseline ({baseline_source}): "
              f"WNS={baseline_metrics['wns']:.3f}ps  "
              f"TNS={baseline_metrics['tns']:.3f}ps")
        if baseline_source == "base":
            print("[autotuner] NOTE: using placement-parasitic base metrics as baseline "
                  "(run --run-stage default for post-GR baseline)")
    else:
        print("[autotuner] WARNING: no baseline metrics found — objective will fail")
    print(f"{'='*70}\n")

    # --- Optuna study ---
    # Use RDBStorage with SQLite timeout so concurrent set_user_attr() writes
    # from worker threads retry instead of raising "database is locked".
    study_name = f"autotuner_{design}_{pdk}"
    storage    = optuna.storages.RDBStorage(
        url=f"sqlite:///{output_dir}/optuna_study.db",
        engine_kwargs={"connect_args": {"timeout": 60}},
    )

    if resume:
        try:
            study = optuna.load_study(study_name=study_name, storage=storage)
            print(f"[autotuner] Resumed '{study_name}' — "
                  f"{len(study.trials)} prior trials")
        except Exception as e:
            print(f"[autotuner] WARNING: resume failed ({e}), creating fresh study")
            resume = False

    if not resume:
        try:
            optuna.delete_study(study_name=study_name, storage=storage)
        except Exception:
            pass
        study = optuna.create_study(
            study_name=study_name,
            direction="maximize",
            storage=storage,
            sampler=optuna.samplers.TPESampler(
                seed=42,
                n_startup_trials=n_startup_trials,
            ),
        )

    # --- Executor import (from autotuner's own scripts/python — standalone) ---
    # Per-PDK module so the dont-use list cannot get mixed up between PDKs.
    _scripts_py = autotuner_root / "scripts" / "python"
    if str(_scripts_py) not in sys.path:
        sys.path.insert(0, str(_scripts_py))
    if pdk == "nangate45":
        import executor_nangate45 as _executor
    else:
        import executor as _executor

    # --- Objective (closure over all config) ---
    def objective(trial: optuna.Trial,
                  _precomputed=None,
                  _trial_dir: pathlib.Path = None) -> float:
        """
        _precomputed : (sequence, knobs) pre-decoded in main thread (Phase 2)
        _trial_dir   : persistent directory for this trial's files
                       (startup_trials/test_trial_N or main_trials/iteration_N/trial_N)
        """
        trial_idx = trial.number
        stage_tag = f"trial_{trial_idx:03d}"

        # Temp dir inside output_dir so Docker can reach it via /workspace mount.
        # Deleted after metrics collected; persistent files go to _trial_dir.
        tmp_dir = output_dir / f"_tmp_{stage_tag}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tcl_file    = tmp_dir / "run_plan.tcl"
        output_odb  = tmp_dir / "output.odb"
        timing_file = tmp_dir / f"{stage_tag}_timing.txt"

        # Params: use pre-decoded if provided (Phase 2), else suggest here (Phase 1)
        if _precomputed is not None:
            sequence, knobs = _precomputed
        else:
            sequence, knobs = decode_params(trial, valid_moves)
            trial.set_user_attr("sequence",       ",".join(sequence))
            trial.set_user_attr("repair_tns",     knobs["repair_tns"])
            trial.set_user_attr("max_passes",     knobs["max_passes"])
            trial.set_user_attr("max_iterations", knobs["max_iterations"])
            trial.set_user_attr("setup_margin",   knobs["setup_margin"])

        # Generate TCL
        plan_dict = {
            "plan_type": "sequence",
            "sequence":  sequence,
            "run_knobs": knobs,
        }
        tcl_file.write_text(_executor.generate_run_plan_tcl(plan_dict, trial_idx, 0))

        # Docker command — INPUT_DB points directly to base_cts.odb (no copy)
        env_vars = {
            "INPUT_DB":                 _to_docker(base_cts_odb),
            "OUTPUT_DB":                _to_docker(output_odb),
            "OUTPUT_DIR":               _to_docker(tmp_dir) + "/",
            "SDC_FILE":                 _to_docker(sdc_file),
            "LEF_DIR":                  _to_docker(lef_dir) + "/",
            "LIB_DIR":                  _to_docker(lib_dir) + "/",
            "SETRC_TCL":                _to_docker(setrc_tcl),
            "PDK_PREAMBLE":             _to_docker(pdk_preamble),
            "STAGE_TAG":                stage_tag,
            "FLOW_HOME":                docker_flow_home,
            "MIN_ROUTING_LAYER":        pdk_cfg.min_routing_layer,
            "MAX_ROUTING_LAYER":        pdk_cfg.max_routing_layer,
            "ROUTING_LAYER_ADJUSTMENT": str(pdk_cfg.routing_layer_adjustment),
        }

        cmd = ["docker", "run", "--rm", "--user", "root"]
        for k, v in env_vars.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += ["-v", f"{workspace}:/workspace", "-w", "/workspace/experiments/autotuner_rsz"]
        cmd += [docker_image, docker_openroad, "-no_init", "-exit", _to_docker(tcl_file)]

        # Execute Docker
        start_time_str = time.strftime("%Y-%m-%d %H:%M:%S")
        start      = time.time()
        status_str = "infra_error"
        log_text   = ""
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            log_text = proc.stdout + proc.stderr
            elapsed  = time.time() - start
            status_str, _ = _classify_error(log_text, timing_file)
        except Exception as e:
            elapsed    = time.time() - start
            log_text   = str(e)
            status_str = "infra_error"

        # Collect metrics while temp files still exist
        wns = tns = area = util = power = None
        objective_val = ERROR_METRIC

        if status_str == "success":
            m = _collect_metrics(tmp_dir, stage_tag, time_unit_ps)
            wns, tns, area, util, power = (
                m["wns"], m["tns"], m["area_um2"], m["util_pct"], m["power_W"]
            )
            if wns is not None:
                objective_val = _compute_objective(wns, tns, power, baseline_metrics)
                trial.set_user_attr("wns_ps",    wns)
                trial.set_user_attr("tns_ps",    tns or 0.0)
                trial.set_user_attr("area_um2",  area  or 0.0)
                trial.set_user_attr("util_pct",  util  or 0.0)
                trial.set_user_attr("power_W",   power or 0.0)
                trial.set_user_attr("objective", objective_val)
            else:
                status_str = "metrics_missing"

        trial.set_user_attr("status",    status_str)
        trial.set_user_attr("runtime_s", f"{elapsed:.1f}")

        # --- Persist trial files to the correct directory ---
        if _trial_dir is None:
            _trial_dir = output_dir / f"trial_{trial_idx:03d}"
        _trial_dir.mkdir(parents=True, exist_ok=True)

        # Copy reports from tmp
        for fname in [f"{stage_tag}_timing.txt", f"{stage_tag}_area.rpt",
                      f"{stage_tag}_power.rpt"]:
            src = tmp_dir / fname
            if src.exists():
                shutil.copy2(src, _trial_dir / fname)

        # trial.log — full OpenROAD stdout/stderr
        (_trial_dir / "trial.log").write_text(log_text)

        # params.csv — what the autotuner suggested
        with open(_trial_dir / "params.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sequence", "repair_tns", "max_passes",
                        "max_iterations", "setup_margin"])
            w.writerow([",".join(sequence),
                        f"{knobs['repair_tns']:.2f}",
                        knobs["max_passes"],
                        knobs["max_iterations"],
                        f"{knobs['setup_margin']:.2f}"])

        # metrics.csv — OpenROAD output
        with open(_trial_dir / "metrics.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["wns_ps", "tns_ps", "area_um2", "util_pct",
                        "power_W", "score", "status"])
            w.writerow([f"{wns:.3f}"   if wns   is not None else "",
                        f"{tns:.3f}"   if tns   is not None else "",
                        f"{area:.2f}"  if area  is not None else "",
                        f"{util:.2f}"  if util  is not None else "",
                        f"{power:.6e}" if power is not None else "",
                        f"{objective_val:.4f}" if objective_val > ERROR_METRIC else "FAILED",
                        status_str])

        # runtime.csv
        with open(_trial_dir / "runtime.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["runtime_s", "start_time"])
            w.writerow([f"{elapsed:.1f}", start_time_str])

        # Delete tmp dir
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

        # --- Top-level CSVs (combined view across all trials) ---
        _write_params_row(params_csv, trial,
                          trial.user_attrs.get("iteration_label", "startup"),
                          valid_moves)

        _append_result_row(results_csv, {
            "trial_idx":      trial_idx,
            "sequence":       ",".join(sequence),
            "repair_tns":     f"{knobs['repair_tns']:.2f}",
            "max_passes":     knobs["max_passes"],
            "max_iterations": knobs["max_iterations"],
            "setup_margin":   f"{knobs['setup_margin']:.2f}",
            "wns_ps":         f"{wns:.3f}"   if wns   is not None else "",
            "tns_ps":         f"{tns:.3f}"   if tns   is not None else "",
            "area_um2":       f"{area:.2f}"  if area  is not None else "",
            "util_pct":       f"{util:.2f}"  if util  is not None else "",
            "power_W":        f"{power:.6e}" if power is not None else "",
            "objective":      f"{objective_val:.4f}" if objective_val > ERROR_METRIC else "FAILED",
            "status":         status_str,
            "runtime_s":      f"{elapsed:.1f}",
        })

        # Live progress
        best_str = ""
        try:
            bv = study.best_value
            if bv > ERROR_METRIC:
                best_str = f"  [best score={bv:.4f}]"
        except Exception:
            pass

        wns_str = f"{wns:.3f}ps" if wns is not None else "FAILED"
        obj_str = f"{objective_val:.4f}" if objective_val > ERROR_METRIC else "FAILED"
        print(f"[autotuner] T{trial_idx:03d}  seq=[{','.join(sequence)}]  "
              f"WNS={wns_str}  score={obj_str}  "
              f"status={status_str}  t={elapsed:.0f}s{best_str}")

        return objective_val

    # --- Run optimization ---
    total_start = time.time()

    if finalize:
        n_complete = sum(1 for t in study.trials
                         if t.state == optuna.trial.TrialState.COMPLETE)
        print(f"[autotuner] --finalize: skipping Phase 1/2, "
              f"{n_complete} completed trials in study")
    else:
        # When resuming, calculate remaining work to reach the original target
        n_completed = sum(1 for t in study.trials
                          if t.state == optuna.trial.TrialState.COMPLETE) if resume else 0
        startup_to_run    = max(0, n_startup_trials - n_completed)
        main_trials_done  = max(0, n_completed - n_startup_trials)
        iterations_done   = main_trials_done // n_jobs
        iterations_to_run = max(0, n_iterations - iterations_done)
        iteration_offset  = iterations_done

        if resume:
            print(f"[autotuner] Resume: {n_completed} trials done — "
                  f"{startup_to_run} startup + {iterations_to_run} iterations remaining")

        # Phase 1: startup trials. TPE uses uniform random sampling until
        # n_startup_trials are completed, so trials have no inter-dependency
        # and can run concurrently. Same ask + main-thread decode + parallel
        # objective pattern as Phase 2 (workers receive pre-decoded params so
        # trial.suggest_*() is never called from a worker thread — that would
        # race on the Optuna SQLite storage).
        if startup_to_run > 0:
            print(f"[autotuner] Phase 1: {startup_to_run} startup trials × {n_jobs} parallel")
            startup_start = time.time()
            completed_startup = 0
            while completed_startup < startup_to_run:
                batch_size = min(n_jobs, startup_to_run - completed_startup)
                batch_params = []
                for _ in range(batch_size):
                    t = study.ask()
                    t.set_user_attr("iteration_label", "startup")
                    seq, knobs = decode_params(t, valid_moves)
                    t.set_user_attr("sequence",       ",".join(seq))
                    t.set_user_attr("repair_tns",     knobs["repair_tns"])
                    t.set_user_attr("max_passes",     knobs["max_passes"])
                    t.set_user_attr("max_iterations", knobs["max_iterations"])
                    t.set_user_attr("setup_margin",   knobs["setup_margin"])
                    batch_params.append((t, seq, knobs))
                with ThreadPoolExecutor(max_workers=batch_size) as ex:
                    futures = {}
                    for offset, (t, seq, knobs) in enumerate(batch_params):
                        idx_global = n_completed + completed_startup + offset + 1
                        trial_dir  = output_dir / "startup_trials" / f"test_trial_{idx_global}"
                        futures[ex.submit(objective, t, (seq, knobs), trial_dir)] = t
                    for f in as_completed(futures):
                        study.tell(futures[f], f.result())
                completed_startup += batch_size
            startup_elapsed = time.time() - startup_start
            _write_runtime_row(runtimes_csv, "autotuner_startup",
                               startup_elapsed, time.strftime("%Y-%m-%d %H:%M:%S"))
            print(f"[autotuner] Startup complete in {startup_elapsed:.0f}s")
        else:
            print(f"[autotuner] Phase 1: skipped ({n_completed} trials already in study)")

        # Phase 2: iterations (batched parallel — TPE model active)
        if iterations_to_run > 0:
            print(f"[autotuner] Phase 2: {iterations_to_run} iterations × {n_jobs} parallel")
        for iteration in range(iterations_to_run):
            iter_label = f"iteration_{iteration_offset + iteration + 1}"
            print(f"[autotuner] --- Iteration {iteration_offset + iteration + 1}/{iteration_offset + iterations_to_run} ---")

            # Time TPE suggestion + param decode together (all main-thread SQLite writes)
            suggest_start = time.time()
            batch = [study.ask() for _ in range(n_jobs)]
            # Decode params in main thread — avoids concurrent trial.suggest_*() SQLite
            # writes from worker threads which cause "database is locked" errors.
            batch_params = []
            for t in batch:
                t.set_user_attr("iteration_label", iter_label)
                seq, knobs = decode_params(t, valid_moves)
                t.set_user_attr("sequence",       ",".join(seq))
                t.set_user_attr("repair_tns",     knobs["repair_tns"])
                t.set_user_attr("max_passes",     knobs["max_passes"])
                t.set_user_attr("max_iterations", knobs["max_iterations"])
                t.set_user_attr("setup_margin",   knobs["setup_margin"])
                batch_params.append((t, seq, knobs))
            suggestion_time = time.time() - suggest_start

            # Run n_jobs trials in parallel — workers receive pre-decoded params + trial dir
            iter_start = time.strftime("%Y-%m-%d %H:%M:%S")
            wall_start  = time.time()
            iter_base_dir = output_dir / "main_trials" / f"iteration_{iteration_offset + iteration + 1}"
            with ThreadPoolExecutor(max_workers=n_jobs) as ex:
                futures = {
                    ex.submit(objective, t, (seq, knobs),
                              iter_base_dir / f"trial_{j + 1}"): t
                    for j, (t, seq, knobs) in enumerate(batch_params)
                }
                for f in as_completed(futures):
                    study.tell(futures[f], f.result())
            wall_elapsed = time.time() - wall_start

            # Max trial runtime (parallel wall-clock = max of individual runtimes)
            trial_runtimes = [float(t.user_attrs.get("runtime_s", 0)) for t in batch]
            max_trial_time = max(trial_runtimes) if trial_runtimes else 0.0
            iter_total     = suggestion_time + max_trial_time

            # Write all tracking CSVs
            _write_runtime_row(runtimes_csv, iter_label, iter_total, iter_start)
            _write_iter_metrics(iter_metrics_csv, iteration_offset + iteration + 1, batch, study)
            _write_summary_row(summary_csv, iteration_offset + iteration + 1, batch, study,
                               suggestion_time, max_trial_time, iter_total)

            print(f"[autotuner] Iteration {iteration_offset + iteration + 1} done — "
                  f"suggest={suggestion_time:.1f}s  "
                  f"max_trial={max_trial_time:.0f}s  "
                  f"total={iter_total:.0f}s")

    total_elapsed = time.time() - total_start
    _write_runtime_row(runtimes_csv, "autotuner_total",
                       total_elapsed, time.strftime("%Y-%m-%d %H:%M:%S"))

    # --- Post-run summary ---
    completed = [t for t in study.trials
                 if t.state == optuna.trial.TrialState.COMPLETE]
    if not completed:
        print("[autotuner] ERROR: no trials completed successfully.")
        return

    best_trial = study.best_trial
    best_seq   = best_trial.user_attrs.get("sequence", "").split(",")
    best_knobs = {
        "repair_tns":     best_trial.user_attrs.get("repair_tns",     100),
        "max_passes":     best_trial.user_attrs.get("max_passes",     10000),
        "max_iterations": best_trial.user_attrs.get("max_iterations", 50000),
        "setup_margin":   best_trial.user_attrs.get("setup_margin",   0.0),
    }
    best_wns   = best_trial.user_attrs.get("wns_ps")
    best_tns   = best_trial.user_attrs.get("tns_ps")
    best_tag   = f"autotuner_trial_{best_trial.number:03d}"
    best_odb   = output_dir / "best_output.odb"
    best_tcl   = output_dir / "best_run_plan.tcl"

    # Re-run best trial params once to produce best_output.odb
    print(f"[autotuner] Re-running best trial #{best_trial.number} to produce best_output.odb ...")
    best_plan = {
        "plan_type": "sequence",
        "sequence":  best_seq,
        "run_knobs": best_knobs,
    }
    best_tcl.write_text(_executor.generate_run_plan_tcl(best_plan, best_trial.number, 0))

    best_stage_tag = f"best_trial_{best_trial.number:03d}"
    best_env = {
        "INPUT_DB":                 _to_docker(base_cts_odb),
        "OUTPUT_DB":                _to_docker(best_odb),
        "OUTPUT_DIR":               _to_docker(output_dir) + "/",
        "SDC_FILE":                 _to_docker(sdc_file),
        "LEF_DIR":                  _to_docker(lef_dir) + "/",
        "LIB_DIR":                  _to_docker(lib_dir) + "/",
        "SETRC_TCL":                _to_docker(setrc_tcl),
        "PDK_PREAMBLE":             _to_docker(pdk_preamble),
        "STAGE_TAG":                best_stage_tag,
        "FLOW_HOME":                docker_flow_home,
        "MIN_ROUTING_LAYER":        pdk_cfg.min_routing_layer,
        "MAX_ROUTING_LAYER":        pdk_cfg.max_routing_layer,
        "ROUTING_LAYER_ADJUSTMENT": str(pdk_cfg.routing_layer_adjustment),
    }
    best_cmd = ["docker", "run", "--rm", "--user", "root"]
    for k, v in best_env.items():
        best_cmd += ["-e", f"{k}={v}"]
    best_cmd += ["-v", f"{workspace}:/workspace", "-w", "/workspace/experiments/autotuner_rsz"]
    best_cmd += [docker_image, docker_openroad, "-no_init", "-exit", _to_docker(best_tcl)]

    best_log = output_dir / "best_run.log"
    with open(best_log, "w") as lf:
        subprocess.run(best_cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)

    # Collect best metrics from output_dir (best_stage_tag files written there)
    best_metrics = _collect_metrics(output_dir, best_stage_tag, time_unit_ps)
    if best_metrics["wns"] is None:
        # Fall back to stored user_attrs if re-run metrics missing
        best_metrics = {
            "wns":      best_wns,
            "tns":      best_tns,
            "area_um2": best_trial.user_attrs.get("area_um2"),
            "util_pct": best_trial.user_attrs.get("util_pct"),
            "power_W":  best_trial.user_attrs.get("power_W"),
        }

    # Write best.json
    (output_dir / "autotuner_best.json").write_text(json.dumps({
        "trial_idx":       best_trial.number,
        "sequence":        best_seq,
        "run_knobs":       best_knobs,
        "wns_ps":          best_wns,
        "tns_ps":          best_tns,
        "score":           best_trial.value,
        "best_odb":        str(best_odb),
        "best_tcl":        str(best_tcl),
        "total_trials":    len(study.trials),
        "successful":      len(completed),
        "completed_at":    time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))

    _write_comparison_csv(output_dir, baseline_metrics, best_metrics, best_tag)

    # Print final summary
    print(f"\n{'='*70}")
    print(f"[autotuner] COMPLETE — {len(study.trials)} trials in {total_elapsed:.0f}s")
    print(f"[autotuner] Successful: {len(completed)} / {len(study.trials)}")
    print(f"[autotuner] Best trial: #{best_trial.number}  seq=[{','.join(best_seq)}]")
    print(f"[autotuner] Best knobs: "
          f"repair_tns={best_knobs['repair_tns']:.1f}  "
          f"max_passes={best_knobs['max_passes']}  "
          f"max_iterations={best_knobs['max_iterations']}  "
          f"setup_margin={best_knobs['setup_margin']:.1f}ps")
    if best_wns is not None:
        print(f"[autotuner] Best WNS={best_wns:.3f}ps  TNS={best_tns:.3f}ps")
        print(f"[autotuner] Best score={best_trial.value:.4f}  "
              f"(higher = more improvement over baseline)")
    if has_baseline and best_wns is not None:
        delta_wns   = best_wns - baseline_metrics["wns"]
        delta_power = ((best_trial.user_attrs.get("power_W", 0) or 0)
                       - (baseline_metrics.get("power_W") or 0))
        print(f"[autotuner] vs baseline ({baseline_source}): "
              f"WNS {delta_wns:+.3f}ps  power {delta_power:+.4e}W")
    print(f"[autotuner] Results:    {results_csv}")
    print(f"[autotuner] Best JSON:  {output_dir / 'autotuner_best.json'}")
    print(f"[autotuner] Comparison: {output_dir / 'autotuner_comparison.csv'}")
    print(f"[autotuner] Best ODB:   {best_odb}")
    print(f"[autotuner] Best TCL:   {best_tcl}")
    print(f"{'='*70}\n")
