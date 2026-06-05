"""Parse OpenROAD global_route (GRT) log output into a compact structured
summary for the Planner's iteration-N prompt.

Consumes: run_default.log (or any plan's run_plan.log) that contains the
`[INFO GRT-*]` lines emitted by `global_route -verbose`.

Emits:
  - parse_gr_metrics(path)         → dict of metrics
  - format_gr_summary(metrics)     → compact multi-line string for prompts

CLI usage (for ad-hoc inspection):
    python3 parse_gr_metrics.py <log_file>            # prints summary
    python3 parse_gr_metrics.py <log_file> --json     # prints full dict as JSON
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Single-value regexes
# ---------------------------------------------------------------------------

_SINGLE_VALUE_PATTERNS = [
    ("wirelength_um",      r"\[INFO GRT-0018\] Total wirelength:\s+(\d+)\s+um"),
    ("via_count",          r"\[INFO GRT-0111\] Final number of vias:\s+(\d+)"),
    ("routed_nets",        r"\[INFO GRT-0014\] Routed nets:\s+(\d+)"),
    ("usage_3d",           r"\[INFO GRT-0112\] Final usage 3D:\s+(\d+)"),
    ("min_routing_layer",  r"\[INFO GRT-0020\] Min routing layer:\s+(\S+)"),
    ("max_routing_layer",  r"\[INFO GRT-0021\] Max routing layer:\s+(\S+)"),
    ("global_adjustment",  r"\[INFO GRT-0022\] Global adjustment:\s+(\S+)"),
    ("clock_nets",         r"\[INFO GRT-0019\] Found\s+(\d+)\s+clock nets"),
    ("macros",             r"\[INFO GRT-0003\] Macros:\s+(\d+)"),
    ("blockages",          r"\[INFO GRT-0004\] Blockages:\s+(\d+)"),
    ("min_degree",         r"\[INFO GRT-0001\] Minimum degree:\s+(\d+)"),
    ("max_degree",         r"\[INFO GRT-0002\] Maximum degree:\s+(\d+)"),
]

_RUNTIME_PATTERN = re.compile(
    r"\[INFO GRT-0303\] Global routing runtime = (\d+):(\d+):(\d+)")

_EXTRA_ITER_PATTERN = re.compile(
    r"\[INFO GRT-0102\] Start extra iteration (\d+)/(\d+)")


# ---------------------------------------------------------------------------
# Table parsers (GRT-0053 routing resources, GRT-0096 final congestion)
# ---------------------------------------------------------------------------

# Routing resources table rows:
#   metal1     Horizontal          0             0          0.00%
_ROUTING_RESOURCE_ROW = re.compile(
    r"^\s*(\w+)\s+(Horizontal|Vertical)\s+(\d+)\s+(\d+)\s+([-\d.]+)%\s*$")

# Final congestion table rows (layer rows and Total):
#   metal1               0             0            0.00%             0 /  0 /  0
#   Total           257544         97730           37.95%             0 /  0 /  0
_CONGESTION_ROW = re.compile(
    r"^\s*(\w+)\s+(\d+)\s+(\d+)\s+([-\d.]+)%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$")


def _parse_routing_resources(text: str) -> List[Dict]:
    """Lines following `[INFO GRT-0053] Routing resources analysis:` up to
    the next blank line / non-matching row."""
    out: List[Dict] = []
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if "[INFO GRT-0053] Routing resources analysis:" in ln:
            start = i + 1
            break
    if start is None:
        return out
    for ln in lines[start:]:
        m = _ROUTING_RESOURCE_ROW.match(ln)
        if m:
            out.append({
                "layer":          m.group(1),
                "direction":      m.group(2),
                "original":       int(m.group(3)),
                "derated":        int(m.group(4)),
                "reduction_pct":  float(m.group(5)),
            })
        elif ln.startswith("----") or ln.strip() == "":
            if out:
                break
    return out


def _parse_final_congestion(text: str) -> Dict:
    """Block following `[INFO GRT-0096] Final congestion report:`.

    Returns {'layers': [...], 'total': {...}} or {} if the block is absent.
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if "[INFO GRT-0096] Final congestion report:" in ln:
            start = i + 1
            break
    if start is None:
        return {}
    layers: List[Dict] = []
    total: Optional[Dict] = None
    for ln in lines[start:]:
        m = _CONGESTION_ROW.match(ln)
        if m:
            row = {
                "layer":       m.group(1),
                "resource":    int(m.group(2)),
                "demand":      int(m.group(3)),
                "usage_pct":   float(m.group(4)),
                "overflow_h":  int(m.group(5)),
                "overflow_v":  int(m.group(6)),
                "overflow_total": int(m.group(7)),
            }
            if row["layer"] == "Total":
                total = row
                break  # Total is the last row
            layers.append(row)
        elif ln.startswith("----") or ln.strip() == "":
            if layers:
                continue
    result: Dict = {"layers": layers}
    if total is not None:
        result["total"] = total
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_gr_metrics(log_path: pathlib.Path) -> Dict:
    """Parse GRT metrics from an OpenROAD log file.

    Returns {} if no GRT messages are present (global_route did not run).
    Missing fields are absent from the returned dict rather than defaulted.
    """
    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {log_path}")
    text = log_path.read_text(errors="ignore")
    if "[INFO GRT-" not in text:
        return {}

    metrics: Dict = {}

    # Single-value patterns
    for key, pat in _SINGLE_VALUE_PATTERNS:
        m = re.search(pat, text)
        if m:
            v = m.group(1)
            metrics[key] = int(v) if v.isdigit() else v

    # Runtime (HH:MM:SS → seconds)
    m = _RUNTIME_PATTERN.search(text)
    if m:
        h, mi, s = (int(x) for x in m.groups())
        metrics["runtime_s"] = h * 3600 + mi * 60 + s

    # Extra-iteration count (number of `GRT-0102 Start extra iteration N/M` lines)
    iter_matches = _EXTRA_ITER_PATTERN.findall(text)
    metrics["extra_iterations"]       = len(iter_matches)
    metrics["extra_iterations_limit"] = int(iter_matches[-1][1]) if iter_matches else 0

    # Tables
    rr = _parse_routing_resources(text)
    if rr:
        metrics["routing_resources"]       = rr
        metrics["max_reduction_pct"]       = max(r["reduction_pct"] for r in rr)
        worst = max(rr, key=lambda r: r["reduction_pct"])
        metrics["max_reduction_layer"]     = worst["layer"]

    cong = _parse_final_congestion(text)
    if cong:
        metrics["congestion"] = cong
        # Top-3 most-used layers (signal layers with non-zero resource).
        signal_layers = [l for l in cong["layers"] if l["resource"] > 0]
        top_used = sorted(signal_layers, key=lambda l: -l["usage_pct"])[:3]
        metrics["top_congested_layers"] = [
            {"layer": l["layer"], "usage_pct": l["usage_pct"]}
            for l in top_used
        ]
        if "total" in cong:
            metrics["total_overflow"]   = cong["total"]["overflow_total"]
            metrics["total_overflow_h"] = cong["total"]["overflow_h"]
            metrics["total_overflow_v"] = cong["total"]["overflow_v"]
            metrics["overall_usage_pct"] = cong["total"]["usage_pct"]

    return metrics


def format_gr_summary(metrics: Dict) -> str:
    """Compact multi-line summary suitable for pasting into the Planner
    prompt. If `metrics` is empty (GR did not run), returns a short notice."""
    if not metrics:
        return "Global route: no GRT metrics found in log (GR did not run or log is incomplete)."

    lines: List[str] = ["Global route summary:"]

    wl = metrics.get("wirelength_um")
    if wl is not None:
        lines.append(f"  Wirelength:      {wl:>12,} um")

    via = metrics.get("via_count")
    if via is not None:
        lines.append(f"  Vias:            {via:>12,}")

    nets = metrics.get("routed_nets")
    if nets is not None:
        lines.append(f"  Routed nets:     {nets:>12,}")

    u3d = metrics.get("usage_3d")
    if u3d is not None:
        lines.append(f"  3D usage:        {u3d:>12,} tracks")

    usage_pct = metrics.get("overall_usage_pct")
    if usage_pct is not None:
        lines.append(f"  Overall usage:   {usage_pct:>12.2f} %")

    overflow_total = metrics.get("total_overflow")
    if overflow_total is not None:
        h = metrics.get("total_overflow_h", 0)
        v = metrics.get("total_overflow_v", 0)
        status = "clean" if overflow_total == 0 else "CONGESTED"
        lines.append(f"  Overflow (H/V/Tot): {h} / {v} / {overflow_total}  [{status}]")

    top = metrics.get("top_congested_layers")
    if top:
        top_str = ", ".join(f"{l['layer']} {l['usage_pct']:.0f}%" for l in top)
        lines.append(f"  Top congested:   {top_str}")

    if metrics.get("max_reduction_layer"):
        lines.append(f"  Max layer reduction: {metrics['max_reduction_pct']:.1f}% "
                     f"(on {metrics['max_reduction_layer']})")

    ei = metrics.get("extra_iterations")
    if ei is not None and ei > 0:
        lim = metrics.get("extra_iterations_limit", 50)
        lines.append(f"  Extra overflow iters: {ei} / {lim}")

    rt = metrics.get("runtime_s")
    if rt is not None:
        if rt < 60:
            rt_str = f"{rt}s"
        elif rt < 3600:
            rt_str = f"{rt // 60}m{rt % 60}s"
        else:
            rt_str = f"{rt // 3600}h{(rt % 3600) // 60}m"
        lines.append(f"  GR runtime:      {rt_str}")

    mn = metrics.get("min_routing_layer")
    mx = metrics.get("max_routing_layer")
    if mn and mx:
        lines.append(f"  Routing layers:  {mn}–{mx}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: parse_gr_metrics.py <log_file> [--json]", file=sys.stderr)
        return 2
    log = pathlib.Path(args[0])
    as_json = "--json" in args[1:]
    try:
        m = parse_gr_metrics(log)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(m, indent=2))
    else:
        print(format_gr_summary(m))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
