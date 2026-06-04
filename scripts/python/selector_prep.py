"""Pre-compute all mechanical Selector inputs from the filesystem.

This module runs OUTSIDE the Claude sandbox and reads real files directly.
It computes all the mechanical parts of the Selector's job so the LLM only
needs to do semantic reasoning (stuck-path analysis, diagnosis, guidance).

Public API:
    ctx = build_context(iter_dir, design_dir, iterN, pdk_name)
    merge_selector_output(iter_dir, ctx)

SelectorContext fields:
    plan_results      list[dict]   — one entry per plan from iteration_summary.csv
    best_plan         str|None     — name of best successful plan (e.g. "plan2")
    best_wns          float|None   — WNS of best_plan
    best_metrics      dict|None    — full metrics row for best_plan
    regressed         bool         — True if best_plan WNS < seed_wns
    seed_wns          float|None   — WNS at start of this iteration
    iteration_table   list[dict]   — WNS trajectory across all iters including current
    plateau_status    str          — "none"|"soft_plateau"|"hard_plateau"
    delta_wns_this_iter  float|None
    delta_wns_last_iter  float|None
    delta_tns_this_iter  float|None
    delta_tns_last_iter  float|None
    prediction_analysis  list[dict]
    prompt_text       str          — compact text for LLM prompt injection
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class SelectorContext:
    plan_results: List[Dict[str, Any]] = field(default_factory=list)
    best_plan: Optional[str] = None
    best_wns: Optional[float] = None
    best_metrics: Optional[Dict[str, Any]] = None
    regressed: bool = False
    seed_wns: Optional[float] = None
    seed_tns: Optional[float] = None
    iteration_table: List[Dict[str, Any]] = field(default_factory=list)
    plateau_status: str = "none"          # "none"|"soft_plateau"|"hard_plateau"
    delta_wns_this_iter: Optional[float] = None
    delta_wns_last_iter: Optional[float] = None
    delta_tns_this_iter: Optional[float] = None
    delta_tns_last_iter: Optional[float] = None
    prediction_analysis: List[Dict[str, Any]] = field(default_factory=list)
    prompt_text: str = ""


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _read_csv_first_row(path: pathlib.Path) -> Optional[Dict[str, str]]:
    """Return first data row of a CSV as a dict, or None if missing/empty."""
    if not path.exists():
        return None
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                return dict(row)
    except (OSError, csv.Error):
        pass
    return None


def _read_csv_all_rows(path: pathlib.Path) -> List[Dict[str, str]]:
    """Return all rows of a CSV as a list of dicts."""
    if not path.exists():
        return []
    try:
        with open(path, newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except (OSError, csv.Error):
        return []


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# seed_wns resolution
# ---------------------------------------------------------------------------

def _resolve_seed_wns(
    iter_dir: pathlib.Path,
    design_dir: pathlib.Path,
    iterN: int,
) -> Optional[float]:
    """Read the seed WNS for iteration N (in picoseconds, same units as iteration_summary.csv).

    Priority order:
      1. Prior iteration's best_plan.txt wns= field (ps, post-GR) — most reliable for iter > 1.
         For backtrack scenarios, selector_decision.json backtrack_to_iteration tells us which
         prior iter was seeded from; we walk that chain.
      2. Iteration 1: design_dir/base/base_metrics.csv wns column (ps, post-GR).
      3. Fall back: start/seed_metrics.csv — NOTE: this CSV stores pre-repair WNS at
         PLACEMENT parasitics in nanoseconds. Only use if both above are unavailable and
         convert by multiplying by 1000 (ns → ps).

    Important: seed_metrics.csv WNS is in ns (placement parasitics, pre-repair).
    All other WNS values in the LLM loop are in ps (after-GR parasitics, post-repair).
    """
    llm_root = iter_dir.parent  # LLM_iterations/

    if iterN == 1:
        # Iter 1 seeds from base CTS — base_metrics.csv has after-GR-equivalent WNS in ps
        # (actually pre-RT, but at placement parasitics; use best available)
        # Prefer the seed_metrics.csv if it exists and is in ps scale
        seed_csv = iter_dir / "start" / "seed_metrics.csv"
        row = _read_csv_first_row(seed_csv)
        if row is not None:
            wns = _safe_float(row.get("wns"))
            if wns is not None:
                # seed_metrics.csv for iter 1 typically stores pre-repair WNS in ns
                # Convert to ps if the value is clearly in ns (magnitude < 10)
                if wns is not None and abs(wns) < 10.0:
                    wns = round(wns * 1000.0, 3)
                return wns

        base_row = _read_csv_first_row(design_dir / "base" / "base_metrics.csv")
        if base_row:
            wns = _safe_float(base_row.get("wns"))
            # base_metrics.csv also stores in ns (pre-repair, placement parasitics)
            if wns is not None and abs(wns) < 10.0:
                wns = round(wns * 1000.0, 3)
            return wns
        return None

    # Iter > 1: seed is from a prior iteration's best plan (after-GR, in ps).
    # Check if the prior iteration backtracked, which changes which iter we seed from.
    # Walk backwards: prior iter's selector_decision may have backtrack_to_iteration.
    seed_iter = iterN - 1
    prior_sel_path = llm_root / f"Iteration{iterN - 1}" / "selector_decision.json"
    if prior_sel_path.exists():
        try:
            prior_sel = json.loads(prior_sel_path.read_text())
            bt = prior_sel.get("backtrack_to_iteration")
            if isinstance(bt, int) and bt >= 1:
                seed_iter = bt
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    prior_best_txt = llm_root / f"Iteration{seed_iter}" / "best" / "best_plan.txt"
    if prior_best_txt.exists():
        try:
            for line in prior_best_txt.read_text().splitlines():
                if line.startswith("wns="):
                    return _safe_float(line.split("=", 1)[1].strip())
        except OSError:
            pass

    # Fallback: try seed_metrics.csv (convert ns→ps)
    seed_csv = iter_dir / "start" / "seed_metrics.csv"
    row = _read_csv_first_row(seed_csv)
    if row is not None:
        wns = _safe_float(row.get("wns"))
        if wns is not None:
            if abs(wns) < 10.0:
                wns = round(wns * 1000.0, 3)
            return wns

    return None


# ---------------------------------------------------------------------------
# seed_tns resolution (mirrors _resolve_seed_wns)
# ---------------------------------------------------------------------------

def _resolve_seed_tns(
    iter_dir: pathlib.Path,
    design_dir: pathlib.Path,
    iterN: int,
) -> Optional[float]:
    """Read the seed TNS for iteration N (picoseconds, same units as iteration_summary.csv)."""
    llm_root = iter_dir.parent

    if iterN == 1:
        seed_csv = iter_dir / "start" / "seed_metrics.csv"
        row = _read_csv_first_row(seed_csv)
        if row is not None:
            tns = _safe_float(row.get("tns"))
            if tns is not None:
                if abs(tns) < 100.0:
                    tns = round(tns * 1000.0, 3)
                return tns
        base_row = _read_csv_first_row(design_dir / "base" / "base_metrics.csv")
        if base_row:
            tns = _safe_float(base_row.get("tns"))
            if tns is not None and abs(tns) < 100.0:
                tns = round(tns * 1000.0, 3)
            return tns
        return None

    seed_iter = iterN - 1
    prior_sel_path = llm_root / f"Iteration{iterN - 1}" / "selector_decision.json"
    if prior_sel_path.exists():
        try:
            prior_sel = json.loads(prior_sel_path.read_text())
            bt = prior_sel.get("backtrack_to_iteration")
            if isinstance(bt, int) and bt >= 1:
                seed_iter = bt
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    prior_best_txt = llm_root / f"Iteration{seed_iter}" / "best" / "best_plan.txt"
    if prior_best_txt.exists():
        try:
            for line in prior_best_txt.read_text().splitlines():
                if line.startswith("tns="):
                    return _safe_float(line.split("=", 1)[1].strip())
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# plan_results — parse iteration_summary.csv
# ---------------------------------------------------------------------------

def _build_plan_results(
    iter_dir: pathlib.Path,
    seed_wns: Optional[float],
    seed_tns: Optional[float],
    planner_plans: List[Dict],
) -> List[Dict[str, Any]]:
    """Build plan_results list from iteration_summary.csv."""
    rows = _read_csv_all_rows(iter_dir / "iteration_summary.csv")
    results: List[Dict[str, Any]] = []
    for row in rows:
        plan = row.get("plan", "")
        status = row.get("status", "")
        wns = _safe_float(row.get("wns"))
        tns = _safe_float(row.get("tns"))
        area = _safe_float(row.get("area_um2"))
        util = _safe_float(row.get("util_percent"))
        power = _safe_float(row.get("power_mw"))
        viol = _safe_int(row.get("violating_endpoints"))
        delta_wns: Optional[float] = None
        if wns is not None and seed_wns is not None:
            delta_wns = round(wns - seed_wns, 3)
        delta_tns: Optional[float] = None
        if tns is not None and seed_tns is not None:
            delta_tns = round(tns - seed_tns, 3)
        results.append({
            "plan": plan,
            "status": status,
            "wns": wns,
            "tns": tns,
            "delta_tns": delta_tns,
            "area_um2": area,
            "util_percent": util,
            "power_mw": power,
            "violating_endpoints": viol,
            "delta_wns": delta_wns,
            "error_type": row.get("error_type", ""),
            "first_error": row.get("first_error", ""),
        })
    return results


# ---------------------------------------------------------------------------
# best_plan selection
# ---------------------------------------------------------------------------

def _select_best_plan(
    plan_results: List[Dict[str, Any]],
    seed_wns: Optional[float],
) -> tuple:
    """Return (best_plan_name, best_wns, best_metrics, regressed).

    Selection rules:
      - Only successful plans are candidates.
      - Primary: highest WNS (least negative = closest to 0).
      - Tiebreaker within 2.0 ps: lower area_um2.
      - If all successful plans regress, pick least-bad; regressed=True.
    """
    successful = [p for p in plan_results if p.get("status") == "success"
                  and p.get("wns") is not None]
    if not successful:
        return None, None, None, False

    # Sort by WNS descending (best = highest = closest to 0)
    # Tiebreaker: area_um2 ascending (smaller is better)
    def sort_key(p: Dict) -> tuple:
        wns = p["wns"]
        area = p.get("area_um2") or float("inf")
        return (-wns, area)  # negate WNS so sort ascending gives highest WNS first

    # We can't simply sort because the tiebreaker only applies within 2 ps.
    # Instead find the best WNS, then among those within 2 ps pick smallest area.
    best_wns_val = max(p["wns"] for p in successful)
    candidates = [p for p in successful if abs(p["wns"] - best_wns_val) <= 2.0]
    # Among candidates within 2 ps WNS window:
    # - If any candidate has meaningfully better TNS (> 5% improvement over the worst TNS
    #   in the candidate set), prefer it — it clears more competing paths.
    # - Tiebreaker: area ascending.
    tns_vals = [p.get("tns") for p in candidates if p.get("tns") is not None]
    if len(tns_vals) > 1:
        worst_tns = min(tns_vals)  # most negative = worst
        best_tns_val = max(tns_vals)  # least negative = best
        tns_spread = abs(best_tns_val - worst_tns)
        if tns_spread > abs(worst_tns) * 0.05:
            # TNS spread is meaningful — prefer the candidate with best TNS
            candidates.sort(key=lambda p: (
                -(p.get("tns") or float("-inf")),   # best TNS first (least negative)
                p.get("area_um2") or float("inf"),   # then smallest area
            ))
        else:
            candidates.sort(key=lambda p: (p.get("area_um2") or float("inf")))
    else:
        candidates.sort(key=lambda p: (p.get("area_um2") or float("inf")))
    best = candidates[0]

    regressed = False
    if seed_wns is not None and best["wns"] < seed_wns:
        regressed = True

    metrics = {
        "wns": best["wns"],
        "tns": best["tns"],
        "area_um2": best["area_um2"],
        "util_percent": best["util_percent"],
        "power_mw": best["power_mw"],
        "violating_endpoints": best["violating_endpoints"],
    }
    return best["plan"], best["wns"], metrics, regressed


# ---------------------------------------------------------------------------
# iteration_table — WNS trajectory
# ---------------------------------------------------------------------------

def _build_iteration_table(
    design_dir: pathlib.Path,
    iterN: int,
    current_best_plan: Optional[str],
    current_best_wns: Optional[float],
    current_seed_wns: Optional[float],
    plan_results: List[Dict[str, Any]],
    current_best_tns: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Build iteration_table with WNS trajectory for iterations 1..N."""
    llm_root = design_dir / "LLM_iterations"
    table: List[Dict[str, Any]] = []

    # Gather completed prior iterations (1 to N-1)
    for k in range(1, iterN):
        best_txt = llm_root / f"Iteration{k}" / "best" / "best_plan.txt"
        if not best_txt.exists():
            continue
        plan_name = ""
        wns_out: Optional[float] = None
        tns_out: Optional[float] = None
        try:
            for line in best_txt.read_text().splitlines():
                if line.startswith("plan="):
                    plan_name = line.split("=", 1)[1].strip()
                elif line.startswith("wns="):
                    wns_out = _safe_float(line.split("=", 1)[1].strip())
                elif line.startswith("tns="):
                    tns_out = _safe_float(line.split("=", 1)[1].strip())
        except OSError:
            continue

        # Determine wns_in for this prior iteration
        # wns_in of iter 1 = base_metrics WNS
        # wns_in of iter k = wns_out of iter k-1 (or backtrack target)
        # We read it from the selector_decision's iteration_table if available,
        # falling back to seed_metrics.csv
        wns_in: Optional[float] = None
        prior_sel_path = llm_root / f"Iteration{k}" / "selector_decision.json"
        if prior_sel_path.exists():
            try:
                prior_sel = json.loads(prior_sel_path.read_text())
                it_tbl = prior_sel.get("iteration_table") or []
                for entry in it_tbl:
                    if entry.get("iter") == k:
                        wns_in = _safe_float(entry.get("wns_in"))
                        break
            except (OSError, json.JSONDecodeError, KeyError):
                pass

        if wns_in is None:
            # Fall back to seed_metrics.csv
            seed_csv = llm_root / f"Iteration{k}" / "start" / "seed_metrics.csv"
            row = _read_csv_first_row(seed_csv)
            if row:
                wns_in = _safe_float(row.get("wns"))
            elif k == 1:
                base_row = _read_csv_first_row(design_dir / "base" / "base_metrics.csv")
                if base_row:
                    wns_in = _safe_float(base_row.get("wns"))

        delta_wns: Optional[float] = None
        if wns_in is not None and wns_out is not None:
            delta_wns = round(wns_out - wns_in, 3)

        table.append({
            "iter": k,
            "plan": plan_name,
            "wns_in": wns_in,
            "wns_out": wns_out,
            "delta_wns": delta_wns,
            "tns_out": tns_out,
        })

    # Add current iteration
    delta_current: Optional[float] = None
    if current_seed_wns is not None and current_best_wns is not None:
        delta_current = round(current_best_wns - current_seed_wns, 3)

    # Determine sequence string for current best plan
    table.append({
        "iter": iterN,
        "plan": current_best_plan or "",
        "wns_in": current_seed_wns,
        "wns_out": current_best_wns,
        "delta_wns": delta_current,
        "tns_out": current_best_tns,
    })

    return table


# ---------------------------------------------------------------------------
# plateau_status — deterministic
# ---------------------------------------------------------------------------

def _compute_plateau(
    iteration_table: List[Dict[str, Any]],
    delta_wns_this_iter: Optional[float],
    delta_wns_last_iter: Optional[float],
    all_regressed: bool,
    delta_tns_this_iter: Optional[float] = None,
    delta_tns_last_iter: Optional[float] = None,
    seed_tns: Optional[float] = None,
) -> str:
    """Return "none"|"soft_plateau"|"hard_plateau".

    WNS is the primary plateau signal, but significant TNS improvement
    (> 5% of seed TNS per iteration for 2 consecutive iters) prevents a
    WNS plateau from being declared — the design is still converging.
    """
    if all_regressed:
        return "hard_plateau"

    if delta_wns_this_iter is None:
        return "none"

    # Determine whether TNS is making meaningful progress.
    # "Meaningful" = TNS improved > 5% of seed_tns this iter AND last iter.
    tns_improving = False
    if (delta_tns_this_iter is not None
            and delta_tns_last_iter is not None
            and seed_tns is not None
            and seed_tns != 0):
        tns_pct_this = abs(delta_tns_this_iter) / abs(seed_tns)
        tns_pct_last = abs(delta_tns_last_iter) / abs(seed_tns)
        # TNS improving = both iterations showed > 5% reduction in violations
        tns_improving = (delta_tns_this_iter > 0 and tns_pct_this > 0.05
                         and delta_tns_last_iter > 0 and tns_pct_last > 0.05)

    # Hard plateau: |ΔWNS| < 1.0 for last 2 iters
    if (delta_wns_last_iter is not None
            and abs(delta_wns_this_iter) < 1.0
            and abs(delta_wns_last_iter) < 1.0):
        # Downgrade to soft if TNS is still meaningfully improving
        if tns_improving:
            return "soft_plateau"
        return "hard_plateau"

    # Soft plateau: |ΔWNS| < 3.0 for last 2 iters
    if (delta_wns_last_iter is not None
            and abs(delta_wns_this_iter) < 3.0
            and abs(delta_wns_last_iter) < 3.0):
        # Downgrade to none if TNS is still meaningfully improving
        if tns_improving:
            return "none"
        return "soft_plateau"

    return "none"


# ---------------------------------------------------------------------------
# prediction_analysis
# ---------------------------------------------------------------------------

def _parse_predicted_delta(expected_outcome: str) -> Optional[float]:
    """Extract a midpoint ΔWNS from an expected_outcome string.

    Handles patterns like:
      "~15-25 ps"         → 20.0
      "~20 ps"            → 20.0
      "+15-25 ps"         → 20.0
      "ΔWNS ~10-25 ps"    → 17.5
      "improvement ~15ps" → 15.0
    Returns None if no pattern found.
    """
    # Try range pattern: N-M ps
    m = re.search(r'[~+]?\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*ps', expected_outcome, re.I)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo + hi) / 2.0

    # Try single value: ~N ps or +N ps
    m = re.search(r'[~+]\s*(\d+(?:\.\d+)?)\s*ps', expected_outcome, re.I)
    if m:
        return float(m.group(1))

    # Try plain N ps after "improvement"
    m = re.search(r'improvement\s+(\d+(?:\.\d+)?)\s*ps', expected_outcome, re.I)
    if m:
        return float(m.group(1))

    return None


def _classify_prediction(
    predicted_midpoint: Optional[float],
    actual_delta: Optional[float],
) -> str:
    """Return accuracy classification string."""
    if actual_delta is None:
        return "regressed"
    if predicted_midpoint is None:
        # No prediction to compare against
        if actual_delta > 0:
            return "accurate"
        return "regressed"
    if actual_delta < 0:
        return "regressed"
    # Within 50% of predicted midpoint
    if predicted_midpoint > 0 and actual_delta >= predicted_midpoint * 0.5:
        if actual_delta > predicted_midpoint * 1.5:
            return "over_performed"
        return "accurate"
    if actual_delta > predicted_midpoint:
        return "over_performed"
    return "under_performed"


def _build_prediction_analysis(
    plan_results: List[Dict[str, Any]],
    planner_plans: List[Dict],
    seed_wns: Optional[float],
) -> List[Dict[str, Any]]:
    """Compare Planner predictions to actual results."""
    analysis: List[Dict[str, Any]] = []
    for i, pp in enumerate(planner_plans):
        plan_name = f"plan{i + 1}"
        expected = pp.get("expected_outcome", "")
        # Extract sequence string
        pt = pp.get("plan_type", "sequence")
        if pt == "staged":
            sequence = [s.get("move", "") for s in (pp.get("stages") or [])]
        elif pt == "eco":
            sequence = ["eco"]
        else:
            sequence = list(pp.get("sequence") or [])

        predicted_mid = _parse_predicted_delta(expected)

        # Find matching plan result
        actual_delta: Optional[float] = None
        for pr in plan_results:
            if pr.get("plan") == plan_name:
                actual_delta = pr.get("delta_wns")
                break

        accuracy = _classify_prediction(predicted_mid, actual_delta)

        # Generate brief implication
        implication = _generate_implication(
            plan_name, sequence, pt, accuracy,
            predicted_mid, actual_delta, expected,
        )

        entry: Dict[str, Any] = {
            "plan": plan_name,
            "sequence": sequence,
            "predicted": expected,
            "actual_delta_wns": actual_delta,
            "accuracy": accuracy,
            "implication": implication,
        }
        if predicted_mid is not None:
            entry["predicted_midpoint_ps"] = round(predicted_mid, 1)
        analysis.append(entry)
    return analysis


def _generate_implication(
    plan_name: str,
    sequence: List[str],
    plan_type: str,
    accuracy: str,
    predicted_mid: Optional[float],
    actual_delta: Optional[float],
    expected_str: str,
) -> str:
    """Generate a brief human-readable implication string."""
    seq_str = ",".join(sequence) if sequence else plan_type
    if accuracy == "accurate":
        return f"{plan_name} ({seq_str}) performed as predicted — move model is well-calibrated for this cone"
    if accuracy == "over_performed":
        if actual_delta is not None and predicted_mid is not None:
            over = round(actual_delta - predicted_mid, 1)
            return (f"{plan_name} exceeded prediction by {over} ps — "
                    f"this sequence has more headroom than the Planner estimated")
        return f"{plan_name} ({seq_str}) exceeded prediction — more headroom available"
    if accuracy == "under_performed":
        if actual_delta is not None and predicted_mid is not None:
            under = round(predicted_mid - actual_delta, 1)
            first_move = sequence[0] if sequence else seq_str
            return (f"{plan_name} under-performed by {under} ps — "
                    f"{first_move}-first approach did not move the bottleneck as expected")
        return f"{plan_name} ({seq_str}) under-performed — bottleneck more resistant than predicted"
    if accuracy == "regressed":
        return (f"{plan_name} ({seq_str}) made things worse — "
                f"this approach is counter-productive on the current path structure")
    return f"{plan_name} ({seq_str}) result inconclusive"


# ---------------------------------------------------------------------------
# prompt_text formatter
# ---------------------------------------------------------------------------

def _format_prompt_text(
    iterN: int,
    seed_wns: Optional[float],
    plan_results: List[Dict[str, Any]],
    best_plan: Optional[str],
    best_wns: Optional[float],
    regressed: bool,
    iteration_table: List[Dict[str, Any]],
    plateau_status: str,
    delta_wns_this_iter: Optional[float],
    delta_wns_last_iter: Optional[float],
    prediction_analysis: List[Dict[str, Any]],
    planner_plans: List[Dict],
) -> str:
    lines: List[str] = []

    # Seed
    seed_str = f"{seed_wns:.1f}" if seed_wns is not None else "unknown"
    lines.append(f"SEED WNS: {seed_str} ps")
    lines.append("")

    # Plan results
    lines.append(f"PLAN RESULTS (Iteration {iterN}):")
    for pr in plan_results:
        plan = pr["plan"]
        status = pr["status"]
        wns = pr.get("wns")
        tns = pr.get("tns")
        delta = pr.get("delta_wns")
        delta_tns = pr.get("delta_tns")
        viol = pr.get("violating_endpoints")
        wns_str = f"{wns:.1f}" if wns is not None else "N/A"
        delta_str = (f"ΔWNS={delta:+.1f}" if delta is not None else "ΔWNS=N/A")
        tns_str = (f"TNS={tns:.1f}" if tns is not None else "")
        delta_tns_str = (f"ΔTNS={delta_tns:+.1f}" if delta_tns is not None else "")

        # Get sequence string for this plan
        plan_idx = int(plan.replace("plan", "")) - 1 if plan.startswith("plan") else -1
        seq_str = ""
        if 0 <= plan_idx < len(planner_plans):
            pp = planner_plans[plan_idx]
            pt = pp.get("plan_type", "sequence")
            if pt == "staged":
                stages = [s.get("move", "") for s in (pp.get("stages") or [])]
                seq_str = "staged:" + "→".join(stages)
            elif pt == "eco":
                seq_str = "eco"
            else:
                seq_str = ",".join(pp.get("sequence") or [])

        # Prediction accuracy from prediction_analysis
        pred_note = ""
        for pa in prediction_analysis:
            if pa["plan"] == plan:
                mid = pa.get("predicted_midpoint_ps")
                acc = pa.get("accuracy", "")
                if mid is not None:
                    pred_note = f" | predicted ~+{mid:.0f}ps → {acc.replace('_', '-')}"
                elif acc:
                    pred_note = f" | → {acc.replace('_', '-')}"
                break

        line = f"  {plan}"
        if seq_str:
            line += f" | {seq_str}"
        if status == "success":
            tns_part = f" | {tns_str} | {delta_tns_str}" if tns_str else ""
            viol_part = f" | viol={viol}" if viol is not None else ""
            line += f" | WNS={wns_str} | {delta_str}{tns_part}{viol_part} | {status}{pred_note}"
        else:
            line += f" | {status}"
            if pr.get("error_type"):
                line += f" ({pr['error_type']})"
        lines.append(line)
    lines.append("")

    # Best plan
    if best_plan and best_wns is not None:
        best_delta = delta_wns_this_iter
        delta_str = f"ΔWNS={best_delta:+.1f}" if best_delta is not None else ""
        reg_note = " — REGRESSION vs seed" if regressed else " — no regression"
        lines.append(f"BEST PLAN: {best_plan} (WNS={best_wns:.1f} ps"
                     + (f", {delta_str}" if delta_str else "") + f"){reg_note}")
    else:
        lines.append("BEST PLAN: none (all plans failed or regressed)")
    lines.append("")

    # Iteration table
    lines.append("ITERATION TABLE:")
    for row in iteration_table:
        k = row["iter"]
        plan = row.get("plan") or "?"
        wns_in = row.get("wns_in")
        wns_out = row.get("wns_out")
        tns_out = row.get("tns_out")
        delta = row.get("delta_wns")
        in_str = f"{wns_in:.1f}" if wns_in is not None else "?"
        out_str = f"{wns_out:.1f}" if wns_out is not None else "?"
        delta_str = (f"ΔWNS={delta:+.1f}" if delta is not None else "ΔWNS=?")
        tns_str = f" | TNS={tns_out:.1f}" if tns_out is not None else ""
        suffix = " ← this iter" if k == iterN else ""
        lines.append(f"  k={k}: {plan} | in={in_str} → out={out_str} | {delta_str}{tns_str}{suffix}")
    lines.append("")

    # Plateau
    d_str = (f"|ΔWNS|={abs(delta_wns_this_iter):.1f} ps this iter"
             if delta_wns_this_iter is not None else "no successful plans this iter")
    if plateau_status == "hard_plateau":
        lines.append(f"PLATEAU: hard_plateau ({d_str})")
    elif plateau_status == "soft_plateau":
        lines.append(f"PLATEAU: soft_plateau ({d_str})")
    else:
        lines.append(f"PLATEAU: none ({d_str})")
    lines.append("")

    # Prediction calibration
    notable = [pa for pa in prediction_analysis
               if pa.get("accuracy") not in ("accurate",)]
    if notable:
        lines.append("PREDICTION CALIBRATION:")
        for pa in notable:
            impl = pa.get("implication", "")
            acc = pa.get("accuracy", "").replace("_", " ")
            lines.append(f"  {pa['plan']} {acc}: {impl}")
    else:
        lines.append("PREDICTION CALIBRATION: all plans performed as predicted")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public: build_context
# ---------------------------------------------------------------------------

def build_context(
    iter_dir: pathlib.Path,
    design_dir: pathlib.Path,
    iterN: int,
    pdk_name: str = "",
) -> SelectorContext:
    """Pre-compute all mechanical Selector inputs. Returns a SelectorContext."""
    ctx = SelectorContext()

    # 1. Seed WNS and seed TNS
    ctx.seed_wns = _resolve_seed_wns(iter_dir, design_dir, iterN)
    ctx.seed_tns = _resolve_seed_tns(iter_dir, design_dir, iterN)

    # 2. Load planner_decision.json for plan sequences + expected outcomes
    planner_plans: List[Dict] = []
    planner_path = iter_dir / "planner_decision.json"
    if planner_path.exists():
        try:
            pd_json = json.loads(planner_path.read_text())
            planner_plans = pd_json.get("plans") or []
        except (OSError, json.JSONDecodeError):
            pass

    # 3. Plan results from iteration_summary.csv
    ctx.plan_results = _build_plan_results(iter_dir, ctx.seed_wns, ctx.seed_tns, planner_plans)

    # 4. Best plan selection
    ctx.best_plan, ctx.best_wns, ctx.best_metrics, ctx.regressed = _select_best_plan(
        ctx.plan_results, ctx.seed_wns
    )

    # 5. delta_wns_this_iter and delta_tns_this_iter
    if ctx.seed_wns is not None and ctx.best_wns is not None:
        ctx.delta_wns_this_iter = round(ctx.best_wns - ctx.seed_wns, 3)
    else:
        ctx.delta_wns_this_iter = None

    best_tns = ctx.best_metrics.get("tns") if ctx.best_metrics else None
    if ctx.seed_tns is not None and best_tns is not None:
        ctx.delta_tns_this_iter = round(best_tns - ctx.seed_tns, 3)
    else:
        ctx.delta_tns_this_iter = None

    # 6. delta_wns_last_iter and delta_tns_last_iter (from prior iteration's selector_decision)
    if iterN > 1:
        llm_root = design_dir / "LLM_iterations"
        prior_sel_path = llm_root / f"Iteration{iterN - 1}" / "selector_decision.json"
        if prior_sel_path.exists():
            try:
                prior_sel = json.loads(prior_sel_path.read_text())
                conv = prior_sel.get("convergence") or {}
                ctx.delta_wns_last_iter = _safe_float(conv.get("delta_wns_this_iter"))
                ctx.delta_tns_last_iter = _safe_float(conv.get("delta_tns_this_iter"))
            except (OSError, json.JSONDecodeError):
                pass

    # 7. Iteration table
    current_best_tns = ctx.best_metrics.get("tns") if ctx.best_metrics else None
    ctx.iteration_table = _build_iteration_table(
        design_dir=design_dir,
        iterN=iterN,
        current_best_plan=ctx.best_plan,
        current_best_wns=ctx.best_wns,
        current_seed_wns=ctx.seed_wns,
        plan_results=ctx.plan_results,
        current_best_tns=current_best_tns,
    )

    # 8. All-plans-regressed check
    successful = [p for p in ctx.plan_results if p.get("status") == "success"
                  and p.get("wns") is not None]
    all_regressed = False
    if successful and ctx.seed_wns is not None:
        all_regressed = all(p["wns"] < ctx.seed_wns for p in successful)
    elif not successful:
        all_regressed = True  # all plans failed → treat as regressed for plateau purposes

    # 9. Plateau status
    ctx.plateau_status = _compute_plateau(
        ctx.iteration_table,
        ctx.delta_wns_this_iter,
        ctx.delta_wns_last_iter,
        all_regressed,
        delta_tns_this_iter=ctx.delta_tns_this_iter,
        delta_tns_last_iter=ctx.delta_tns_last_iter,
        seed_tns=ctx.seed_tns,
    )

    # 10. Prediction analysis
    ctx.prediction_analysis = _build_prediction_analysis(
        ctx.plan_results, planner_plans, ctx.seed_wns
    )

    # 11. Format prompt_text
    ctx.prompt_text = _format_prompt_text(
        iterN=iterN,
        seed_wns=ctx.seed_wns,
        plan_results=ctx.plan_results,
        best_plan=ctx.best_plan,
        best_wns=ctx.best_wns,
        regressed=ctx.regressed,
        iteration_table=ctx.iteration_table,
        plateau_status=ctx.plateau_status,
        delta_wns_this_iter=ctx.delta_wns_this_iter,
        delta_wns_last_iter=ctx.delta_wns_last_iter,
        prediction_analysis=ctx.prediction_analysis,
        planner_plans=planner_plans,
    )

    return ctx


# ---------------------------------------------------------------------------
# Public: merge_selector_output
# ---------------------------------------------------------------------------

def merge_selector_output(
    iter_dir: pathlib.Path,
    ctx: SelectorContext,
) -> bool:
    """Read the LLM-written selector_decision.json, merge in pre-computed fields,
    and write the merged result back.

    The LLM writes a simplified JSON with only:
        decision, stop_reason, backtrack_to_iteration, strategy_note

    Python fills in all mechanical fields:
        chosen_plan, chosen_metrics, chosen_sequence, regressed, convergence,
        iteration_table, plan_results, strategy_note.prediction_analysis
    Also adds backward-compat: strategy_note.hints_to_planner (alias of guidance).

    Returns True on success, False if the file is missing or unparseable.
    """
    sel_path = iter_dir / "selector_decision.json"
    if not sel_path.exists():
        return False

    try:
        sel = json.loads(sel_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    # Determine iteration number from path name
    iter_name = iter_dir.name  # "Iteration<N>"
    try:
        iterN = int(iter_name.replace("Iteration", ""))
    except ValueError:
        iterN = sel.get("iteration", 0)

    # --- Resolve chosen_sequence from planner_decision ---
    chosen_seq = _resolve_chosen_sequence(iter_dir, ctx.best_plan)

    # --- Merge in pre-computed mechanical fields ---
    sel["iteration"] = iterN
    sel["chosen_plan"] = ctx.best_plan
    sel["chosen_sequence"] = chosen_seq
    sel["chosen_metrics"] = ctx.best_metrics
    sel["regressed"] = ctx.regressed

    # Build convergence dict (canonical schema format)
    plateau_schema_val = _plateau_to_schema_value(ctx.plateau_status, iterN, ctx.delta_wns_this_iter)
    trend = _build_trend(ctx.iteration_table, iterN)
    sel["convergence"] = {
        "delta_wns_this_iter": ctx.delta_wns_this_iter,
        "delta_wns_last_iter": ctx.delta_wns_last_iter,
        "delta_tns_this_iter": ctx.delta_tns_this_iter,
        "delta_tns_last_iter": ctx.delta_tns_last_iter,
        "plateau_status": plateau_schema_val,
        "trend": trend,
    }

    sel["iteration_table"] = [
        {k: v for k, v in row.items()
         if k in ("iter", "plan", "wns_in", "wns_out", "delta_wns", "tns_out")}
        for row in ctx.iteration_table
        if row.get("wns_in") is not None and row.get("wns_out") is not None
    ]

    sel["plan_results"] = [
        {k: v for k, v in pr.items()
         if k in ("plan", "status", "wns", "tns", "delta_wns", "delta_tns", "area_um2", "violating_endpoints")}
        for pr in ctx.plan_results
    ]

    # Clean strategy_note — strip LLM-generated legacy fields
    sn = sel.get("strategy_note") or {}
    sn.pop("hints_to_planner", None)
    sn.pop("prediction_analysis", None)
    sel["strategy_note"] = sn

    # Ensure rationale field exists (for schema compliance — LLM may not write it)
    if "rationale" not in sel:
        parts: List[str] = []
        if ctx.best_plan:
            parts.append(f"{ctx.best_plan} WNS={ctx.best_wns:.1f} ps selected as best")
        if ctx.delta_wns_this_iter is not None:
            parts.append(f"ΔWNS this iter: {ctx.delta_wns_this_iter:+.1f} ps")
        parts.append(f"Plateau status: {ctx.plateau_status}")
        sel["rationale"] = parts or ["(pre-computed by selector_prep.py)"]

    try:
        sel_path.write_text(json.dumps(sel, indent=2))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Helpers for merge_selector_output
# ---------------------------------------------------------------------------

def _resolve_chosen_sequence(
    iter_dir: pathlib.Path,
    best_plan: Optional[str],
) -> Optional[List[str]]:
    """Get the sequence list for the chosen plan from planner_decision.json."""
    if not best_plan:
        return None
    planner_path = iter_dir / "planner_decision.json"
    if not planner_path.exists():
        return None
    try:
        pd_json = json.loads(planner_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    plans = pd_json.get("plans") or []
    try:
        idx = int(best_plan.replace("plan", "")) - 1
    except (ValueError, AttributeError):
        return None
    if idx < 0 or idx >= len(plans):
        return None

    pp = plans[idx]
    pt = pp.get("plan_type", "sequence")
    if pt == "staged":
        return [s.get("move", "") for s in (pp.get("stages") or [])]
    elif pt == "eco":
        return ["eco"]
    else:
        return list(pp.get("sequence") or [])


def _plateau_to_schema_value(
    plateau_status: str,
    iterN: int,
    delta_wns_this_iter: Optional[float],
) -> str:
    """Map internal plateau_status to the schema enum value."""
    if iterN == 1 and delta_wns_this_iter is None:
        return "no_data"
    if plateau_status == "hard_plateau":
        return "hard_plateau"
    if plateau_status == "soft_plateau":
        return "soft_plateau"
    if delta_wns_this_iter is not None and abs(delta_wns_this_iter) > 3.0:
        return "improving"
    if plateau_status == "none":
        if delta_wns_this_iter is None:
            return "no_data"
        return "improving"
    return "no_data"


def _build_trend(
    iteration_table: List[Dict[str, Any]],
    iterN: int,
) -> List[Optional[float]]:
    """Build trend list: ΔWNS for last 1–3 iterations (most recent last)."""
    deltas = [
        row.get("delta_wns")
        for row in iteration_table
        if row.get("delta_wns") is not None
    ]
    # Last 3
    return deltas[-3:] if deltas else [None]
