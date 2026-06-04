#!/usr/bin/env python3
"""Phase-2B artifact: per-experiment decision trajectory.

For a chosen (design, variant), plots WNS over iterations with marker
shape encoding Selector decision and color encoding plateau diagnosis.
Backtrack arrows are drawn from iter N to its backtrack_to_iteration
target.

The trajectory is taken from dialogue_dataset.csv (chosen plan rows only)
so it matches whatever the Selector actually promoted at each step.
"""
import argparse
import csv
import pathlib
import sys
from typing import List, Dict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

ROOT = pathlib.Path(__file__).resolve().parents[3]
DATASET = ROOT / "dialogue_dataset.csv"

STYLE = {
    "axis_label_fontsize": 22,
    "tick_fontsize":       16,
    "legend_fontsize":     14,
    "annot_fontsize":      11,
    "title_fontsize":      20,
    "marker_size":         260,
    "line_color":          "tab:gray",
    "line_alpha":          0.7,
    "line_width":          2.0,
}

DECISION_MARKER = {
    "continue":          "o",   # circle
    "backtrack":         "s",   # square
    "retry":             "^",   # triangle
    "accept_regression": "D",   # diamond
    "stop":              "X",   # cross
    "":                  ".",   # fallback
}

PLATEAU_COLOR = {
    "":                "tab:gray",
    "new_bottleneck":  "tab:blue",
    "same_bottleneck": "tab:orange",
    "broad_stall":     "tab:red",
}


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _load_chosen(design: str, variant: str) -> List[Dict]:
    rows = []
    with DATASET.open() as fh:
        for r in csv.DictReader(fh):
            if r["design"] == design and r["variant"] == variant and r["is_chosen_plan"] == "1":
                rows.append(r)
    rows.sort(key=lambda r: int(r["iteration"]))
    return rows


def plot_case(design: str, variant: str, out_path: pathlib.Path,
              default_wns: float = None) -> bool:
    rows = _load_chosen(design, variant)
    if not rows:
        print(f"  no rows for {design}/{variant}")
        return False

    fig, ax = plt.subplots(figsize=(12, 7))

    iters = [int(r["iteration"]) for r in rows]
    wns   = [_to_float(r["wns_out_ps"]) for r in rows]

    # Connecting line in the background
    ax.plot(iters, wns,
            color=STYLE["line_color"], alpha=STYLE["line_alpha"],
            linewidth=STYLE["line_width"], zorder=1)

    # Per-iteration markers
    for r, x, y in zip(rows, iters, wns):
        if y is None:
            continue
        dec = r["selector_decision"]
        pl  = r["plateau_diagnosis"]
        marker = DECISION_MARKER.get(dec, "o")
        color  = PLATEAU_COLOR.get(pl, "tab:gray")
        ax.scatter([x], [y], s=STYLE["marker_size"], marker=marker,
                   facecolor=color, edgecolor="black", linewidth=1.2,
                   zorder=3)

    # Backtrack arrows
    for r, x, y in zip(rows, iters, wns):
        if r["selector_decision"] != "backtrack":
            continue
        bt = _to_int(r["backtrack_to_iteration"])
        if bt is None or y is None:
            continue
        # Find target y
        tgt_rows = [rr for rr in rows if int(rr["iteration"]) == bt]
        if not tgt_rows:
            continue
        tgt_y = _to_float(tgt_rows[0]["wns_out_ps"])
        if tgt_y is None:
            continue
        ax.annotate("",
                    xy=(bt, tgt_y), xytext=(x, y),
                    arrowprops=dict(arrowstyle="->",
                                    color="tab:red",
                                    linestyle="--",
                                    linewidth=1.5,
                                    alpha=0.7,
                                    connectionstyle="arc3,rad=0.25"),
                    zorder=2)
        ax.annotate(f"BT→{bt}", xy=(x, y),
                    xytext=(5, -16), textcoords="offset points",
                    fontsize=STYLE["annot_fontsize"], color="tab:red")

    # Default-WNS reference line
    if default_wns is not None:
        ax.axhline(default_wns, color="tab:gray", linestyle=":",
                   linewidth=2.0, alpha=0.7,
                   label=f"ORFS default WNS = {default_wns:+.1f} ps")

    # Build legend manually (decisions + plateau colors)
    legend_handles = []
    for dec, mk in DECISION_MARKER.items():
        if dec == "":
            continue
        if any(r["selector_decision"] == dec for r in rows):
            h = plt.Line2D([0], [0], marker=mk, color="w",
                           markerfacecolor="lightgray",
                           markeredgecolor="black",
                           markersize=12, linestyle="None",
                           label=f"Selector: {dec}")
            legend_handles.append(h)
    for pl, col in PLATEAU_COLOR.items():
        if pl == "":
            continue
        if any(r["plateau_diagnosis"] == pl for r in rows):
            h = mpatches.Patch(color=col, label=f"Plateau: {pl}")
            legend_handles.append(h)
    if default_wns is not None:
        legend_handles.append(plt.Line2D(
            [0], [0], color="tab:gray", linestyle=":", linewidth=2,
            label=f"Default WNS = {default_wns:+.1f} ps"))

    ax.legend(handles=legend_handles, loc="best",
              fontsize=STYLE["legend_fontsize"], framealpha=0.9)

    ax.set_xlabel("Iteration", fontsize=STYLE["axis_label_fontsize"])
    ax.set_ylabel("Chosen plan WNS  (ps)", fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    ax.set_title(f"Decision trajectory: {design} / {variant}",
                 fontsize=STYLE["title_fontsize"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", required=True)
    ap.add_argument("--variant", required=True)
    ap.add_argument("--default-wns", type=float, default=None,
                    help="ORFS-default WNS for the horizontal reference line (ps)")
    ap.add_argument("--out",
                    default=None,
                    help="Output PNG path (default: figs/trajectory_<design>_<variant>.png)")
    args = ap.parse_args()

    out = pathlib.Path(args.out) if args.out else (
        ROOT / "figs" / f"trajectory_{args.design}_{args.variant}.png")
    ok = plot_case(args.design, args.variant, out, default_wns=args.default_wns)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
