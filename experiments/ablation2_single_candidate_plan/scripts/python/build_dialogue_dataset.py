#!/usr/bin/env python3
"""Phase-0 survey: build a per-(experiment, iteration, plan) dataset.

Walks both util_sweeps/asap7 and clock_sweep/orfs_fix/asap7 and writes a
single CSV summarizing the Planner→Selector dialogue for every iteration
across every experiment. Downstream artifacts (calibration scatter,
stuck-path table, decision-trajectory plot) consume this.
"""
import argparse
import csv
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[3]
UTIL_SWEEPS  = ROOT / "util_sweeps" / "asap7"
CLOCK_SWEEP  = ROOT / "clock_sweep" / "orfs_fix" / "asap7"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_predicted_delta(expected_outcome: str) -> Optional[float]:
    """Extract a midpoint ΔWNS from a Planner expected_outcome string.

    Mirrors selector_prep._parse_predicted_delta — kept in-file so the survey
    is self-contained.
    """
    if not expected_outcome:
        return None
    m = re.search(r'[~+]?\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*ps',
                  expected_outcome, re.I)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo + hi) / 2.0
    m = re.search(r'[~+]\s*(\d+(?:\.\d+)?)\s*ps', expected_outcome, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r'improvement\s+(\d+(?:\.\d+)?)\s*ps', expected_outcome, re.I)
    if m:
        return float(m.group(1))
    return None


def _classify_prediction(pred_mid: Optional[float], actual: Optional[float]) -> str:
    if actual is None:
        return "no_actual"
    if pred_mid is None:
        return "no_prediction"
    if actual < 0:
        return "regressed"
    if pred_mid > 0 and actual >= pred_mid * 0.5:
        if actual > pred_mid * 1.5:
            return "over_performed"
        return "accurate"
    if actual > pred_mid:
        return "over_performed"
    return "under_performed"


def _plan_sequence(plan: Dict[str, Any]) -> List[str]:
    pt = plan.get("plan_type", "sequence")
    if pt == "staged":
        return [s.get("move", "") for s in (plan.get("stages") or [])]
    if pt == "eco":
        return ["eco"]
    return list(plan.get("sequence") or [])


def _safe_first(lst, default=""):
    if isinstance(lst, list) and lst:
        v = lst[0]
        return v if isinstance(v, str) else str(v)
    return default


def _safe_join(lst, sep=" | ", n: Optional[int] = None) -> str:
    if not isinstance(lst, list):
        return ""
    items = [str(x) for x in lst[: (n if n is not None else len(lst))]]
    return sep.join(items)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

# A "variant_dir" is one experiment instance: e.g. util_sweeps/asap7/ibex/60_750
# Each variant has an LLM_iterations/ tree.

def _find_variants() -> List[Tuple[str, str, str, str, pathlib.Path]]:
    """Return (source, design, sweep_key, variant_label, variant_dir)."""
    out: List[Tuple[str, str, str, str, pathlib.Path]] = []

    if UTIL_SWEEPS.exists():
        for design_dir in sorted(UTIL_SWEEPS.iterdir()):
            if not design_dir.is_dir():
                continue
            for vdir in sorted(design_dir.iterdir()):
                if not vdir.is_dir():
                    continue
                m = re.match(r"(\d+)_(\d+)$", vdir.name)
                if not m:
                    continue
                util_tgt, cp = int(m.group(1)), int(m.group(2))
                out.append(("util_sweep", design_dir.name,
                            f"util={util_tgt}%,CP={cp}",
                            vdir.name, vdir))

    if CLOCK_SWEEP.exists():
        for design_dir in sorted(CLOCK_SWEEP.iterdir()):
            if not design_dir.is_dir():
                continue
            for vdir in sorted(design_dir.iterdir()):
                if not vdir.is_dir():
                    continue
                m = re.match(rf"{re.escape(design_dir.name)}_(\d+)$", vdir.name)
                if not m:
                    # Some clock_sweep dirs have a CP-only suffix (no prefix repeat)
                    m2 = re.match(r"(\d+)$", vdir.name)
                    if not m2:
                        continue
                    cp = int(m2.group(1))
                else:
                    cp = int(m.group(1))
                out.append(("clock_sweep", design_dir.name,
                            f"CP={cp}", vdir.name, vdir))
    return out


def _iter_iterations(variant_dir: pathlib.Path):
    """Yield (n, iter_dir) for Iteration<N> dirs in numerical order.

    Some variants nest under <variant>/<design>/LLM_iterations/...
    """
    candidates = [variant_dir / "LLM_iterations"]
    # Nested-design layout (older util_sweep / jpeg)
    for sub in variant_dir.iterdir() if variant_dir.exists() else []:
        if sub.is_dir() and (sub / "LLM_iterations").exists():
            candidates.append(sub / "LLM_iterations")

    seen = set()
    for llm_root in candidates:
        if not llm_root.exists() or llm_root in seen:
            continue
        seen.add(llm_root)
        for it in sorted(llm_root.iterdir()):
            if not it.is_dir():
                continue
            m = re.match(r"Iteration(\d+)$", it.name)
            if m:
                yield int(m.group(1)), it
        return  # only use first non-empty candidate


# ---------------------------------------------------------------------------
# main row builder
# ---------------------------------------------------------------------------

ROW_COLS = [
    "source", "design", "sweep_key", "variant", "iteration",
    "plan",                       # plan1
    "plan_type", "sequence",
    "predicted_outcome_text",
    "predicted_wns_midpoint_ps",
    "actual_delta_wns_ps",
    "prediction_accuracy",
    "wns_in_ps", "wns_out_ps", "tns_out_ps",
    "violating_endpoints",
    "plan_rating",                # A/B/C/F from Selector
    "plan_rating_note",
    "is_chosen_plan",             # 1 if Selector promoted this plan
    "selector_decision",          # continue / backtrack / stop / accept_regression
    "backtrack_to_iteration",
    "plateau_diagnosis",
    "plateau_status",             # convergence.plateau_status
    "stuck_path_top",
    "selector_guidance_top",      # first guidance bullet
    "selector_avoid_top",         # first avoid_strategies bullet
    "delta_wns_this_iter",
    "delta_wns_last_iter",
    "planner_rationale_top",      # first rationale bullet
]


def _row_for(plan_idx: int,
             planner: Dict[str, Any],
             selector: Dict[str, Any],
             *,
             source: str, design: str, sweep_key: str, variant: str,
             iteration: int) -> Dict[str, Any]:
    plans = planner.get("plans") or []
    plan = plans[plan_idx] if plan_idx < len(plans) else {}
    plan_name = f"plan{plan_idx + 1}"

    seq = _plan_sequence(plan)
    expected = plan.get("expected_outcome", "")
    predicted_mid = _parse_predicted_delta(expected)

    plan_results = selector.get("plan_results") or []
    pr = next((x for x in plan_results if x.get("plan") == plan_name), {})

    accuracy = _classify_prediction(predicted_mid, pr.get("delta_wns"))

    ratings = selector.get("per_plan_ratings") or []
    rating_row = next((x for x in ratings if x.get("plan") == plan_name), {})

    iter_table = selector.get("iteration_table") or []
    cur_row = next((x for x in iter_table if x.get("iter") == iteration), {})

    strat = selector.get("strategy_note") or {}
    convergence = selector.get("convergence") or {}

    rationale = planner.get("rationale") or []

    return {
        "source": source, "design": design, "sweep_key": sweep_key,
        "variant": variant, "iteration": iteration,
        "plan": plan_name,
        "plan_type": plan.get("plan_type", ""),
        "sequence": ",".join(seq),
        "predicted_outcome_text": expected.replace("\n", " ").strip(),
        "predicted_wns_midpoint_ps":
            f"{predicted_mid:.2f}" if predicted_mid is not None else "",
        "actual_delta_wns_ps":
            f"{pr.get('delta_wns'):.3f}" if pr.get("delta_wns") is not None else "",
        "prediction_accuracy": accuracy,
        "wns_in_ps":
            f"{cur_row.get('wns_in'):.3f}" if cur_row.get("wns_in") is not None else "",
        "wns_out_ps":
            f"{pr.get('wns'):.3f}" if pr.get("wns") is not None else "",
        "tns_out_ps":
            f"{pr.get('tns'):.3f}" if pr.get("tns") is not None else "",
        "violating_endpoints":
            pr.get("violating_endpoints", ""),
        "plan_rating": rating_row.get("rating", ""),
        "plan_rating_note":
            (rating_row.get("note") or "").replace("\n", " ").strip(),
        "is_chosen_plan":
            1 if selector.get("chosen_plan") == plan_name else 0,
        "selector_decision": selector.get("decision", ""),
        "backtrack_to_iteration":
            selector.get("backtrack_to_iteration") or "",
        "plateau_diagnosis": strat.get("plateau_diagnosis", ""),
        "plateau_status": convergence.get("plateau_status", ""),
        "stuck_path_top": _safe_first(strat.get("stuck_paths")),
        "selector_guidance_top":
            _safe_first(strat.get("guidance")).replace("\n", " ").strip()[:600],
        "selector_avoid_top":
            _safe_first(strat.get("avoid_strategies")).replace("\n", " ").strip()[:300],
        "delta_wns_this_iter": convergence.get("delta_wns_this_iter", ""),
        "delta_wns_last_iter": convergence.get("delta_wns_last_iter", ""),
        "planner_rationale_top":
            _safe_first(rationale).replace("\n", " ").strip()[:600],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default=str(ROOT / "dialogue_dataset.csv"),
                    help="Output CSV path")
    args = ap.parse_args()

    variants = _find_variants()
    print(f"Found {len(variants)} variants")

    rows: List[Dict[str, Any]] = []
    skipped_no_planner = 0
    skipped_no_selector = 0

    for src, design, sweep_key, variant, vdir in variants:
        for n, idir in _iter_iterations(vdir):
            pj = idir / "planner_decision.json"
            sj = idir / "selector_decision.json"
            if not pj.exists():
                skipped_no_planner += 1
                continue
            if not sj.exists():
                skipped_no_selector += 1
                continue
            try:
                planner = json.loads(pj.read_text())
                selector = json.loads(sj.read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"  parse error in {idir}: {e}", file=sys.stderr)
                continue

            for i in range(len(planner.get("plans") or [])):
                rows.append(_row_for(
                    i, planner, selector,
                    source=src, design=design, sweep_key=sweep_key,
                    variant=variant, iteration=n))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_COLS)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {len(rows)} rows to {out}")
    print(f"  skipped (missing planner_decision): {skipped_no_planner}")
    print(f"  skipped (missing selector_decision): {skipped_no_selector}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
