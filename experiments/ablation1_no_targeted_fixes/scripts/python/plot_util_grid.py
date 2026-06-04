#!/usr/bin/env python3
"""2-row x N-column util-sweep grid: twin-y scatter.

Per panel:
  - x-axis:   effective clock period (CP - WNS), ps
  - left y:   power (mW)         — marker 'o'
  - right y:  target util (%)    — marker '^'
  - series:   baseline (gray) + best/rank1 (blue), scatter only (no lines)

Top row = repair-stage, bottom row = detail-route-stage, columns = designs.
Reuses helpers from plot_util_sweep.py and plot_pareto_grid.py without
modifying them.
"""
import argparse
import pathlib
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, FormatStrFormatter

from plot_util_sweep import (
    UTIL_SWEEPS,
    RANK_FILES,
    STYLE,
    _parse_stage_row,
    _resolve_design,
    _variants_for,
)
from plot_pareto_grid import _ticks_in_range


# Manual tick overrides keyed by (design, stage, axis).
# axis ∈ {"x", "y_power", "y_util"}
CUSTOM_TICKS = {}

POWER_MARKER = "o"
UTIL_MARKER = "^"


def _gather_dual_series(design: str, stage: str):
    """Return label -> list of (util_pct, eff_cp_ps, power_mW)
    restricted to baseline + best/rank1."""
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name)
    out = defaultdict(list)
    if not variants:
        return out

    for util_pct, cp, vdir in variants:
        for label, fname_tmpl, _c, _m in RANK_FILES[:2]:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            out[label].append((util_pct, cp - wns_ps, power_w * 1000.0))
    return out


def _plot_util_panel(ax, design: str, stage: str,
                     title: str | None = None) -> tuple:
    """Render one (design, stage) twin-y scatter panel.

    Returns (ok, ax2) — ax2 is the right-y twin (util %) for legend gathering.
    """
    series = _gather_dual_series(design, stage)
    if not any(series.values()):
        return False, None

    baseline_label, _, baseline_color, _ = RANK_FILES[0]
    rank1_label,    _, rank1_color,    _ = RANK_FILES[1]

    ax2 = ax.twinx()  # right y for util %
    s = STYLE.get("marker_size", 8) ** 2

    power_xs_all, power_ys_all = [], []
    util_xs_all, util_ys_all = [], []

    for lbl, color in [
        (baseline_label, baseline_color),
        (rank1_label,    rank1_color),
    ]:
        pts = series.get(lbl, [])
        if not pts:
            continue
        xs = [p[1] for p in pts]   # eff_cp ps
        power_ys = [p[2] for p in pts]  # mW
        util_ys = [p[0] for p in pts]   # %

        ax.scatter(xs, power_ys, marker=POWER_MARKER, color=color, s=s,
                   label=f"{lbl} — power", zorder=3)
        ax2.scatter(xs, util_ys, marker=UTIL_MARKER, color=color, s=s,
                    label=f"{lbl} — util", zorder=3,
                    facecolors="none", edgecolors=color, linewidths=1.6)

        power_xs_all.extend(xs); power_ys_all.extend(power_ys)
        util_xs_all.extend(xs); util_ys_all.extend(util_ys)

    # Auto-padding on shared x and each y.
    if power_xs_all:
        x_lo, x_hi = min(power_xs_all + util_xs_all), max(power_xs_all + util_xs_all)
        xspan = x_hi - x_lo or 1.0
        ax.set_xlim(x_lo - xspan * 0.08, x_hi + xspan * 0.08)
    if power_ys_all:
        y_lo, y_hi = min(power_ys_all), max(power_ys_all)
        yspan = y_hi - y_lo or 1.0
        ax.set_ylim(y_lo - yspan * 0.10, y_hi + yspan * 0.10)
    if util_ys_all:
        u_lo, u_hi = min(util_ys_all), max(util_ys_all)
        uspan = u_hi - u_lo or 1.0
        ax2.set_ylim(u_lo - uspan * 0.10, u_hi + uspan * 0.10)

    # Tick selection with optional overrides.
    xkey = (design, stage, "x")
    yp_key = (design, stage, "y_power")
    yu_key = (design, stage, "y_util")

    xmin, xmax = ax.get_xlim()
    if xkey in CUSTOM_TICKS:
        xt = CUSTOM_TICKS[xkey]
        ax.set_xlim(min(xmin, xt[0]), max(xmax, xt[-1]))
    else:
        xt = _ticks_in_range(xmin, xmax, n=4, allow_decimals=False)

    ymin, ymax = ax.get_ylim()
    if yp_key in CUSTOM_TICKS:
        ypt = CUSTOM_TICKS[yp_key]
        ax.set_ylim(min(ymin, ypt[0]), max(ymax, ypt[-1]))
    else:
        ypt = _ticks_in_range(ymin, ymax, n=4, allow_decimals=False)

    umin, umax = ax2.get_ylim()
    if yu_key in CUSTOM_TICKS:
        yut = CUSTOM_TICKS[yu_key]
        ax2.set_ylim(min(umin, yut[0]), max(umax, yut[-1]))
    else:
        yut = _ticks_in_range(umin, umax, n=4, allow_decimals=False)

    ax.xaxis.set_major_locator(FixedLocator(xt))
    ax.yaxis.set_major_locator(FixedLocator(ypt))
    ax2.yaxis.set_major_locator(FixedLocator(yut))
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax2.yaxis.set_major_formatter(FormatStrFormatter("%.0f"))

    if title is not None:
        ax.set_title(title, fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax2.tick_params(axis="y", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)

    return True, ax2


def plot_grid_util(designs: list, out_path: pathlib.Path) -> bool:
    """2-row x N-column twin-y scatter: top=repair, bottom=DR.
    x=eff_CP ps, left y=power mW, right y=target util %."""
    n = len(designs)
    if n == 0:
        print("  no designs provided")
        return False

    fig, axes = plt.subplots(2, n, figsize=(7 * n, 12), squeeze=False)
    twin_axes = [[None] * n for _ in range(2)]
    any_ok = False
    for col, d in enumerate(designs):
        title = d[:-2] if d.endswith("_w") else d
        for row, stage in enumerate(("repair", "dr")):
            ok, ax2 = _plot_util_panel(
                axes[row, col], d, stage,
                title=title if row == 0 else None,
            )
            twin_axes[row][col] = ax2
            if not ok:
                print(f"  no {stage} data for '{d}'")
            any_ok = any_ok or ok

    if not any_ok:
        plt.close(fig)
        return False

    # Collect a 4-entry legend (baseline-power, baseline-util, rank1-power, rank1-util).
    handles, labels = [], []
    seen = set()
    for row in range(2):
        for col in range(n):
            for ax in (axes[row, col], twin_axes[row][col]):
                if ax is None:
                    continue
                hs, ls = ax.get_legend_handles_labels()
                for h, l in zip(hs, ls):
                    if l not in seen:
                        seen.add(l)
                        handles.append(h)
                        labels.append(l)

    fig.supxlabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    fig.supylabel("Power (mW)",
                  fontsize=STYLE["axis_label_fontsize"], x=0.06)
    # Right-side super-y label for util.
    fig.text(0.985, 0.5, "Target util (%)",
             rotation=270, ha="center", va="center",
             fontsize=STYLE["axis_label_fontsize"])

    fig.tight_layout()
    fig.subplots_adjust(left=0.11, right=0.93, top=0.88, hspace=0.28)
    if handles:
        fig.legend(handles, labels,
                   loc="upper right", ncol=len(labels),
                   bbox_to_anchor=(0.995, 1.0),
                   frameon=True, framealpha=0.9,
                   fontsize=STYLE["legend_fontsize"])

    top_bbox = axes[0, 0].get_position()
    bot_bbox = axes[1, 0].get_position()
    row_label_x = 0.030
    row_label_style = dict(
        rotation=90, ha="center", va="center",
        fontsize=STYLE["axis_label_fontsize"],
        fontweight="bold", fontstyle="italic", color="#222222",
        bbox=dict(boxstyle="round,pad=0.4",
                  facecolor="#e6e6e6", edgecolor="#888888", linewidth=1.0),
    )
    fig.text(row_label_x, (top_bbox.y0 + top_bbox.y1) / 2,
             "Stage: Repair", **row_label_style)
    fig.text(row_label_x, (bot_bbox.y0 + bot_bbox.y1) / 2,
             "Stage: Detail Route", **row_label_style)

    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--designs", nargs="*", default=None,
                    help="Designs to plot as columns (default: aes gcd ibex jpeg)")
    ap.add_argument("--out-dir", default=str(UTIL_SWEEPS),
                    help="Output directory for the PNG")
    ap.add_argument("--grid-out", default=None,
                    help="Explicit output PNG path "
                         "(default: <out-dir>/util_grid_twin_scatter.png)")
    args = ap.parse_args()

    designs = args.designs or ["aes", "gcd", "ibex", "jpeg"]
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out = (pathlib.Path(args.grid_out).resolve() if args.grid_out
           else out_dir / "util_grid_twin_scatter.png")
    print(f"[util-grid / twin-y scatter / {len(designs)} designs]")
    ok = plot_grid_util(designs, out)
    print(f"\n{int(ok)}/1 plots written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
