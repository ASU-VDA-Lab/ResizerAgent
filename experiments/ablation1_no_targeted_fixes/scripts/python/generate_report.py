#!/usr/bin/env python3
"""
generate_report.py — Compiled text report for a 4-agent timing-closure run.

Usage (standard work_dir layout):
    python3 scripts/python/generate_report.py \
        --design <design> --agent <agent> --pdk <asap7|nangate45> \
        --orfs <old|new|fix> [--output report.txt]

Usage (explicit path — clock_sweep or custom layouts):
    python3 scripts/python/generate_report.py \
        --wdir /path/to/design/workdir --design <csv_prefix> [--output report.txt]

    The --design value is used only to locate CSV files named
    {design}_baseline.csv, {design}_agentic.csv, etc.
    When --wdir is given, --agent / --pdk are ignored.
"""

import argparse
import csv
import json
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent  # 2A-new-context/


def _work_dir(agent: str, pdk: str, design: str) -> pathlib.Path:
    return ROOT / "work_dir" / agent / f"orfs_{orfs}" / pdk / design


def _iter_dir(wdir: pathlib.Path, n: int) -> pathlib.Path:
    return wdir / "LLM_iterations" / f"Iteration{n}"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
SEP  = "─" * 72
SEP2 = "═" * 72


def _rj(v: Any, w: int, fmt: str = ".3f") -> str:
    """Right-justify a numeric value, or 'N/A'."""
    if v in (None, "N/A", ""):
        return "N/A".rjust(w)
    try:
        return f"{float(v):{w}{fmt}}"
    except Exception:
        return str(v).rjust(w)


def _trunc(s: str, n: int = 110) -> str:
    return (s[:n] + "…") if len(s) > n else s


def _read_json(p: pathlib.Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _read_csv(p: pathlib.Path) -> List[Dict]:
    try:
        with open(p, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _plan_desc(plan: dict) -> str:
    """One-line description of a planner plan entry."""
    ptype = plan.get("plan_type", "?")
    if ptype == "sequence":
        moves = plan.get("sequence", [])
        knobs = plan.get("run_knobs", {})
        knob_parts = [f"{k}={v}" for k, v in knobs.items()
                      if k in ("repair_tns", "max_passes", "max_iterations", "setup_margin")]
        knob_str = ("  [" + ", ".join(knob_parts) + "]") if knob_parts else ""
        return f"sequence: {', '.join(moves)}{knob_str}"
    elif ptype == "staged":
        stages = plan.get("stages", [])
        moves = [s.get("move", "?") for s in stages]
        return f"staged ({len(stages)} stages): {', '.join(moves)}"
    return ptype


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _section_base(wdir: pathlib.Path, lines: list) -> None:
    lines.append(f"\n{SEP}")
    lines.append("  BASE METRICS  (post-CTS · no repair_timing · placement parasitics)")
    lines.append(SEP)
    rows = _read_csv(wdir / "base" / "base_metrics.csv")
    if not rows:
        lines.append("  [not found]"); return
    r = rows[0]
    lines.append(f"  WNS : {r.get('wns','N/A')} ps")
    lines.append(f"  TNS : {r.get('tns','N/A')} ps")
    lines.append(f"  Viol: {r.get('violating_endpoints','N/A')}")
    lines.append(f"  Area: {r.get('area_um2','N/A')} um²  "
                 f"Util: {r.get('util_percent','N/A')}%  "
                 f"Power: {r.get('power_mw','N/A')} mW")


def _section_default(wdir: pathlib.Path, lines: list) -> None:
    lines.append(f"\n{SEP}")
    lines.append("  DEFAULT METRICS  (ORFS repair_timing + GR · after-GR parasitics)")
    lines.append(SEP)
    rows = _read_csv(wdir / "default" / "default_metrics.csv")
    if not rows:
        lines.append("  [not found]"); return
    r = rows[0]
    lines.append(f"  WNS : {r.get('wns','N/A')} ps")
    lines.append(f"  TNS : {r.get('tns','N/A')} ps")
    lines.append(f"  Viol: {r.get('violating_endpoints','N/A')}")
    lines.append(f"  Area: {r.get('area_um2','N/A')} um²  "
                 f"Util: {r.get('util_percent','N/A')}%  "
                 f"Power: {r.get('power_mw','N/A')} mW")


def _section_iterations(wdir: pathlib.Path, lines: list) -> list:
    """Emit per-iteration detail. Returns trajectory list for summary table."""
    llm_root = wdir / "LLM_iterations"
    if not llm_root.exists():
        return []

    iters = sorted(
        int(d.name.replace("Iteration", ""))
        for d in llm_root.iterdir()
        if d.is_dir() and d.name.startswith("Iteration") and d.name[9:].isdigit()
    )

    trajectory: List[Dict] = []
    seed_wns_map: Dict[int, Optional[float]] = {}

    for n in iters:
        idir = llm_root / f"Iteration{n}"
        lines.append(f"\n{SEP}")
        lines.append(f"  ITERATION {n}")
        lines.append(SEP)

        # ── Seed ──────────────────────────────────────────────────────────────
        seed_rows = _read_csv(idir / "start" / "seed_metrics.csv")
        seed_wns: Optional[float] = None
        if seed_rows:
            sr = seed_rows[0]
            raw = sr.get("wns", "N/A")
            try:
                seed_wns = float(raw)
            except Exception:
                pass
            lines.append(f"  Seed : {sr.get('stage','?')}  WNS={raw} ps  "
                         f"TNS={sr.get('tns','N/A')} ps  Viol={sr.get('violating_endpoints','N/A')}")
        else:
            lines.append("  Seed : [not found]")
        seed_wns_map[n] = seed_wns

        # ── Planner ───────────────────────────────────────────────────────────
        planner = _read_json(idir / "planner_decision.json")
        if planner:
            plans = planner.get("plans", [])
            lines.append(f"\n  Planner  →  {planner.get('plan_count', len(plans))} plan(s)")
            rationale = planner.get("rationale", [])
            if rationale:
                for b in rationale[:2]:
                    lines.append(f"    • {_trunc(b, 120)}")
            for i, p in enumerate(plans, 1):
                desc = _plan_desc(p)
                lines.append(f"    Plan {i}  {desc}")
                exp = p.get("expected_outcome", "")
                if exp:
                    lines.append(f"           → {_trunc(exp, 110)}")
        else:
            lines.append("\n  Planner : [not found]")

        # ── Iteration results ─────────────────────────────────────────────────
        sum_rows = _read_csv(idir / "iteration_summary.csv")
        if sum_rows:
            lines.append(f"\n  Results:")
            lines.append(f"    {'Plan':<8} {'Status':<10} {'WNS(ps)':>10} "
                         f"{'TNS(ps)':>15} {'Area':>8} {'Util':>6} {'Viol':>6}")
            lines.append(f"    {'-'*8} {'-'*10} {'-'*10} {'-'*15} {'-'*8} {'-'*6} {'-'*6}")
            for row in sum_rows:
                plan   = row.get("plan", "?")
                status = row.get("status", "?")
                wns    = row.get("wns", "N/A")
                tns    = row.get("tns", "N/A")
                area   = row.get("area_um2", "N/A")
                util   = row.get("util_percent", "N/A")
                viol   = row.get("violating_endpoints", "N/A")
                err    = row.get("first_error", "")
                wns_s  = _rj(wns,  10)
                tns_s  = _rj(tns,  15)
                area_s = _rj(area,  8, ".0f")
                util_s = _rj(util,  6, ".0f")
                viol_s = ("N/A".rjust(6) if viol in ("N/A","")
                          else str(int(float(viol))).rjust(6))
                err_s  = f"  [{_trunc(err, 60)}]" if err else ""
                lines.append(f"    {plan:<8} {status:<10} {wns_s} {tns_s} "
                             f"{area_s} {util_s}% {viol_s}{err_s}")
        else:
            lines.append("  Results : [not found]")

        # ── Selector ──────────────────────────────────────────────────────────
        selector = _read_json(idir / "selector_decision.json")
        chosen_wns: Optional[float] = None
        chosen_plan = "?"
        decision = "?"

        if selector:
            decision    = selector.get("decision", "?")
            chosen_plan = selector.get("chosen_plan") or "?"
            sn          = selector.get("strategy_note", {})
            plateau     = sn.get("plateau_diagnosis") or "none"
            stuck       = sn.get("stuck_paths", [])
            guidance    = sn.get("guidance", [])
            avoid       = sn.get("avoid_strategies", [])
            stop_reason = selector.get("stop_reason", "")
            backtrack   = selector.get("backtrack_to_iteration")

            # ΔWNS from seed
            if sum_rows:
                for row in sum_rows:
                    if row.get("plan") == chosen_plan:
                        try:
                            chosen_wns = float(row.get("wns", ""))
                        except Exception:
                            pass
                        break

            delta_str = ""
            if seed_wns is not None and chosen_wns is not None:
                delta_str = f"  ΔWNS: {chosen_wns - seed_wns:+.1f} ps"

            lines.append(f"\n  Selector →  decision={decision}   chosen={chosen_plan}{delta_str}")
            if stop_reason:
                lines.append(f"    Stop reason  : {stop_reason}")
            if backtrack is not None:
                lines.append(f"    Backtrack to : Iteration {backtrack}")
            lines.append(f"    Plateau      : {plateau}   "
                         f"Stuck paths: {len(stuck)}")
            if stuck:
                for sp in stuck[:3]:
                    lines.append(f"      • {_trunc(sp, 100)}")
            if guidance:
                lines.append(f"    Guidance ({len(guidance)} bullets):")
                for g in guidance[:3]:
                    lines.append(f"      • {_trunc(g, 120)}")
                if len(guidance) > 3:
                    lines.append(f"      … +{len(guidance)-3} more")
            if avoid:
                lines.append(f"    Avoid ({len(avoid)}):")
                for a in avoid[:3]:
                    lines.append(f"      ✗ {_trunc(a, 110)}")
                if len(avoid) > 3:
                    lines.append(f"      … +{len(avoid)-3} more")
        else:
            lines.append("\n  Selector : [not found]")

        trajectory.append({
            "iter":       n,
            "seed_wns":   seed_wns,
            "chosen":     chosen_plan,
            "chosen_wns": chosen_wns,
            "decision":   decision,
            "plateau":    (selector or {}).get("strategy_note", {}).get("plateau_diagnosis") or "none",
        })

    return trajectory


def _section_trajectory(trajectory: list, lines: list) -> None:
    if not trajectory:
        return
    lines.append(f"\n{SEP}")
    lines.append("  WNS TRAJECTORY SUMMARY")
    lines.append(SEP)
    lines.append(f"  {'Iter':>4}  {'Seed WNS':>11}  {'Chosen':>8}  "
                 f"{'Best WNS':>11}  {'ΔWNS':>8}  {'Decision':<14}  Plateau")
    lines.append(f"  {'----':>4}  {'-'*11}  {'-'*8}  {'-'*11}  {'-'*8}  {'-'*14}  -------")
    for t in trajectory:
        sw  = _rj(t["seed_wns"],   11)
        cw  = _rj(t["chosen_wns"], 11)
        if t["seed_wns"] is not None and t["chosen_wns"] is not None:
            dv = f"{t['chosen_wns'] - t['seed_wns']:>+8.1f}"
        else:
            dv = "     N/A"
        lines.append(f"  {t['iter']:>4}  {sw}  {t['chosen']:>8}  "
                     f"{cw}  {dv}  {t['decision']:<14}  {t['plateau']}")


def _section_rankings(wdir: pathlib.Path, lines: list) -> None:
    d = _read_json(wdir / "LLM_iterations" / "Best_solutions" / "rankings.json")
    if not d:
        return
    lines.append(f"\n{SEP}")
    lines.append("  BEST SOLUTIONS RANKINGS")
    lines.append(SEP)
    lines.append(f"  Last updated: iter {d.get('last_updated_iteration','?')}  "
                 f"({d.get('last_updated','?')})")
    lines.append(f"  {'Rank':>4}  {'Iter':>4}  {'Plan':>8}  {'WNS(ps)':>11}  "
                 f"{'TNS(ps)':>15}  {'Chosen':>8}  ODB")
    lines.append(f"  {'----':>4}  {'----':>4}  {'----':>8}  {'-'*11}  {'-'*15}  {'-'*8}  ---")
    for r in d.get("rankings", []):
        wns    = _rj(r.get("wns_ps"), 11)
        tns    = _rj(r.get("tns_ps"), 15)
        chosen = "✓" if r.get("chosen") else ""
        odb    = "✓" if r.get("odb_exists") else "✗ MISSING"
        lines.append(f"  {r.get('rank','?'):>4}  {r.get('iteration','?'):>4}  "
                     f"{r.get('plan','?'):>8}  {wns}  {tns}  {chosen:>8}  {odb}")


def _section_flow_csv(path: pathlib.Path, title: str, lines: list) -> None:
    rows = _read_csv(path)
    if not rows:
        return
    lines.append(f"\n{SEP}")
    lines.append(f"  {title}")
    lines.append(SEP)
    lines.append(f"  {'Stage':<28} {'WNS(ps)':>12} {'TNS(ps)':>16} "
                 f"{'Area(um²)':>10} {'Util':>7} {'Power(W)':>12}")
    lines.append(f"  {'-'*28} {'-'*12} {'-'*16} {'-'*10} {'-'*7} {'-'*12}")
    for row in rows:
        stage = row.get("stage", "?")
        wns   = _rj(row.get("wns_ps"),   12)
        tns   = _rj(row.get("tns_ps"),   16)
        area  = _rj(row.get("area_um2"), 10, ".1f")
        util  = _rj(row.get("util_pct"),  7, ".1f")
        power = _rj(row.get("power_W"),  12, ".4e")
        lines.append(f"  {stage:<28} {wns} {tns} {area} {util}% {power}")


def _section_comparison(wdir: pathlib.Path, design: str, lines: list) -> None:
    candidates = [
        ("Baseline",   wdir / f"{design}_baseline.csv"),
        ("Best/Rank1", wdir / f"{design}_agentic.csv"),
        ("Rank2",      wdir / f"{design}_agentic_r2.csv"),
        ("Rank3",      wdir / f"{design}_agentic_r3.csv"),
        ("Rank4",      wdir / f"{design}_agentic_r4.csv"),
    ]
    results = []
    for label, path in candidates:
        if not path.exists():
            continue
        rows = _read_csv(path)
        dr = next((r for r in rows if r.get("stage") == "dr_post_route"), None)
        if dr:
            results.append((label, dr))

    if not results:
        return

    baseline_wns: Optional[float] = None
    for label, r in results:
        if label == "Baseline":
            try:
                baseline_wns = float(r.get("wns_ps", ""))
            except Exception:
                pass
            break

    lines.append(f"\n{SEP}")
    lines.append("  COMPARISON: DR POST-ROUTE  (baseline vs agentic ranks)")
    lines.append(SEP)
    lines.append(f"  {'Flow':<14} {'WNS(ps)':>12} {'TNS(ps)':>16} "
                 f"{'Area(um²)':>10} {'Util':>7} {'ΔWNS vs baseline':>18}")
    lines.append(f"  {'-'*14} {'-'*12} {'-'*16} {'-'*10} {'-'*7} {'-'*18}")

    for label, r in results:
        wns  = _rj(r.get("wns_ps"),   12)
        tns  = _rj(r.get("tns_ps"),   16)
        area = _rj(r.get("area_um2"), 10, ".1f")
        util = _rj(r.get("util_pct"),  7, ".1f")

        if label == "Baseline":
            delta_s = "             (ref)"
        elif baseline_wns is not None:
            try:
                delta_s = f"{float(r.get('wns_ps','')) - baseline_wns:>+18.3f}"
            except Exception:
                delta_s = "               N/A"
        else:
            delta_s = "               N/A"

        lines.append(f"  {label:<14} {wns} {tns} {area} {util}% {delta_s}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Generate compiled design run report")
    ap.add_argument("--design", required=True,
                    help="Design name (also used as CSV prefix: {design}_baseline.csv etc.)")
    ap.add_argument("--wdir",   default=None,
                    help="Explicit path to the design work directory (overrides --agent/--pdk)")
    ap.add_argument("--agent",  default="claude")
    ap.add_argument("--pdk",    default="asap7", choices=["asap7", "nangate45"])
    ap.add_argument("--output", default=None,
                    help="Write report to file instead of stdout")
    args = ap.parse_args()

    if args.wdir:
        wdir = pathlib.Path(args.wdir).resolve()
    else:
        wdir = _work_dir(args.agent, args.pdk, args.design)

    if not wdir.exists():
        print(f"[ERROR] work directory not found: {wdir}", file=sys.stderr)
        sys.exit(1)

    lines: List[str] = []

    # Header
    lines.append(SEP2)
    lines.append("  DESIGN RUN REPORT")
    lines.append(f"  Design    : {args.design}")
    if args.wdir:
        lines.append(f"  Work dir  : {wdir}")
    else:
        lines.append(f"  PDK       : {args.pdk}")
        lines.append(f"  Agent     : {args.agent}")
    lines.append(f"  Generated : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(SEP2)

    # Pre-LLM stages
    _section_base(wdir, lines)
    _section_default(wdir, lines)

    # Iteration detail
    trajectory = _section_iterations(wdir, lines)

    # Summaries
    _section_trajectory(trajectory, lines)
    _section_rankings(wdir, lines)

    # Flow CSVs
    flow_specs = [
        (wdir / f"{args.design}_baseline.csv",
         "BASELINE FLOW  (fp → gp → dp → cts → default_repair → grt → dr)"),
        (wdir / f"{args.design}_agentic.csv",
         "AGENTIC FLOW — BEST / RANK 1  (fp → gp → dp → cts → agentic → grt → dr)"),
        (wdir / f"{args.design}_agentic_r2.csv",
         "AGENTIC FLOW — RANK 2"),
        (wdir / f"{args.design}_agentic_r3.csv",
         "AGENTIC FLOW — RANK 3"),
        (wdir / f"{args.design}_agentic_r4.csv",
         "AGENTIC FLOW — RANK 4"),
    ]
    for path, title in flow_specs:
        if path.exists():
            _section_flow_csv(path, title, lines)

    # Comparison
    _section_comparison(wdir, args.design, lines)

    # Footer
    lines.append(f"\n{SEP2}")
    lines.append("  END OF REPORT")
    lines.append(SEP2)

    report = "\n".join(lines) + "\n"

    if args.output:
        out_path = pathlib.Path(args.output)
        out_path.write_text(report)
        print(f"Report written → {out_path}")
    else:
        print(report)


if __name__ == "__main__":
    main()
