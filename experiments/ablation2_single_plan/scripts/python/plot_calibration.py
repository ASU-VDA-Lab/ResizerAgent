#!/usr/bin/env python3
"""Phase-1A artifact: Planner prediction-calibration scatter.

Reads dialogue_dataset.csv and plots predicted ΔWNS (midpoint of the
Planner's expected_outcome range) against actual ΔWNS. Colours encode
the over/under/accurate/regressed accuracy tag. A diagonal y = x reference
line marks perfect prediction.

Also prints mean |error| split by iteration bucket so the writeup can
quantify within-run learning.
"""
import argparse
import csv
import pathlib
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[3]
DATASET = ROOT / "dialogue_dataset.csv"

STYLE = {
    "axis_label_fontsize": 24,
    "tick_fontsize":       18,
    "legend_fontsize":     18,
    "title_fontsize":      22,
    "marker_size":         60,
    "marker_alpha":        0.55,
    "diag_linewidth":      2.0,
    "diag_color":          "black",
}

TAG_STYLE = {
    "accurate":       {"color": "tab:green",  "marker": "o", "label": "Accurate (±50% of midpoint)"},
    "over_performed": {"color": "tab:blue",   "marker": "^", "label": "Over-performed"},
    "under_performed":{"color": "tab:orange", "marker": "s", "label": "Under-performed"},
    "regressed":      {"color": "tab:red",    "marker": "x", "label": "Regressed (Δ<0)"},
}


def _load_rows(path: pathlib.Path):
    return list(csv.DictReader(path.open()))


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--out",
                    default=str(ROOT / "figs" / "calibration_scatter.png"))
    ap.add_argument("--chosen-only", action="store_true",
                    help="Only plot plans the Selector promoted (filters to ~half the data)")
    ap.add_argument("--xlim", nargs=2, type=float, default=[-25, 50])
    ap.add_argument("--ylim", nargs=2, type=float, default=[-70, 70])
    args = ap.parse_args()

    rows = _load_rows(pathlib.Path(args.dataset))
    if args.chosen_only:
        rows = [r for r in rows if r["is_chosen_plan"] == "1"]

    # Bucket rows by accuracy tag
    pts_by_tag = defaultdict(list)
    iter_buckets = defaultdict(list)   # iter_bucket → list of |error|
    for r in rows:
        pm = _to_float(r["predicted_wns_midpoint_ps"])
        ad = _to_float(r["actual_delta_wns_ps"])
        tag = r["prediction_accuracy"]
        if pm is None or ad is None or tag not in TAG_STYLE:
            continue
        it = int(r["iteration"])
        pts_by_tag[tag].append((pm, ad, it))
        iter_buckets["1-3" if it <= 3 else ("4-7" if it <= 7 else "8+")].append(abs(pm - ad))

    if not any(pts_by_tag.values()):
        print("No data after filtering", file=sys.stderr)
        return 1

    fig, ax = plt.subplots(figsize=(11, 9))

    # Plot per-tag in z-order so green (accurate) ends up on top
    z_order = ["regressed", "under_performed", "over_performed", "accurate"]
    for tag in z_order:
        pts = pts_by_tag.get(tag, [])
        if not pts:
            continue
        style = TAG_STYLE[tag]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.scatter(xs, ys, s=STYLE["marker_size"],
                   alpha=STYLE["marker_alpha"],
                   c=style["color"], marker=style["marker"],
                   edgecolors="none",
                   label=f"{style['label']} (n={len(pts)})")

    # y = x reference
    lo = min(args.xlim[0], args.ylim[0])
    hi = max(args.xlim[1], args.ylim[1])
    ax.plot([lo, hi], [lo, hi],
            color=STYLE["diag_color"], linewidth=STYLE["diag_linewidth"],
            linestyle="--", label="y = x  (perfect prediction)")
    ax.axhline(0, color="gray", linewidth=0.6, alpha=0.5)
    ax.axvline(0, color="gray", linewidth=0.6, alpha=0.5)

    ax.set_xlim(*args.xlim)
    ax.set_ylim(*args.ylim)
    ax.set_xlabel("Predicted ΔWNS midpoint  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.set_ylabel("Actual ΔWNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    title_suffix = " (chosen plans only)" if args.chosen_only else " (all plans)"
    ax.set_title(f"Planner prediction calibration{title_suffix}",
                 fontsize=STYLE["title_fontsize"])
    ax.legend(loc="lower right", framealpha=0.9,
              fontsize=STYLE["legend_fontsize"])

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"written: {out}")

    # Print summary stats for the writeup
    print()
    print("=== mean |predicted - actual|  (ps), by iteration bucket ===")
    for bucket in ["1-3", "4-7", "8+"]:
        vals = iter_buckets.get(bucket, [])
        if not vals:
            continue
        mae = sum(vals) / len(vals)
        med = sorted(vals)[len(vals) // 2]
        print(f"  iter {bucket:<3}  n={len(vals):4d}   mean|err|={mae:6.2f}   median={med:6.2f}")

    # And by tag
    print()
    print("=== rows per accuracy tag (after filter) ===")
    for tag in z_order:
        n = len(pts_by_tag.get(tag, []))
        print(f"  {tag:<16} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
