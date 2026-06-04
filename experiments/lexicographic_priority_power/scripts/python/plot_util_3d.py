#!/usr/bin/env python3
"""2-row x N-column util-sweep grid: 3-D curves.

Per panel:
  - x-axis:  target util (%)
  - y-axis:  effective clock period (CP - WNS, ps)
  - z-axis:  power (mW)
  - series:  baseline (gray) + best/rank1 (blue), one connected 3-D curve each

Each util point is a 3-D marker; markers are connected by a line in 3-D, with
vertical stems dropped to the floor (xy plane at z_min) and a dashed shadow of
the curve traced on the floor for legibility.

Top row = repair-stage, bottom row = detail-route-stage, columns = designs.
Reuses helpers from plot_util_sweep.py without modifying it.
"""
import argparse
import pathlib
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FixedLocator, FormatStrFormatter
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (registers 3d projection)

from plot_util_sweep import (
    UTIL_SWEEPS,
    RANK_FILES,
    STYLE,
    _parse_stage_row,
    _resolve_design,
    _variants_for,
)
from plot_pareto_grid import _ticks_in_range


AXIS_COLORS = {
    "x": "#D9531E",  # orange — Target util
    "y": "#27AE60",  # green  — Effective CP   (green chosen to avoid clashing with the blue rank1 series)
    "z": "#8E44AD",  # purple — Power
}

# Extra gap between tick labels and each axis line. z gets a larger pad because
# the panel rotation puts z labels right up against the left pane.
AXIS_TICK_PAD = {"x": 2, "y": 2, "z": 16}

# Per-design clock period (ps) — appended to the column title as "(CP <n>ps)".
DESIGN_CLOCK_PS = {
    "aes":  240,
    "gcd":  240,
    "ibex": 750,
    "jpeg": 430,
}


def _design_title(design: str) -> str:
    """Strip the optional _w suffix and append the clock period if known."""
    base = design[:-2] if design.endswith("_w") else design
    cp = DESIGN_CLOCK_PS.get(base)
    return f"{base} (CP {cp}ps)" if cp is not None else base

# Per-design (optionally per-stage) explicit tick overrides. Two accepted
# forms — pick whichever you need per design:
#   "design":  {"y": [...], "z": [...]}                    # both stages
#   "design":  {"repair": {"y":...,"z":...},
#              "dr":     {"y":...,"z":...}}                # per stage
# Order: x = util %, y = effective CP (ps), z = power (mW).
PER_DESIGN_TICKS = {
    "aes": {
        "repair": {"x": [45, 50, 55, 60, 65, 70], "y": [288, 291, 294, 297], "z": [358, 366, 374, 382]},
        "dr": {"x": [45, 50, 55, 60, 65, 70], "y": [281, 284, 287, 290], "z": [360, 364, 368, 372]},
    },
    "gcd":  {
        "repair": {"x": [45, 50, 55, 60, 65, 70], "z": [1.4, 1.45, 1.5, 1.55]},
        "dr": {"x": [45, 50, 55, 60, 65, 70], "y": [244, 245.5, 247, 248.5], "z": [1.41, 1.44, 1.47, 1.5]},
    },
    "ibex": {
        "repair": {"x": [30, 40, 50, 60, 70, 75], "y": [855, 975, 1095, 1215], "z": [40, 43, 46, 49]},
        "dr": {"x": [30, 40, 50, 60, 70, 75], },
    },
    "jpeg": {
        "repair": {"x": [30, 40, 50, 60, 70, 80], "y": [855, 975, 1095, 1215],},
        "dr": {"x": [30, 40, 50, 60, 70, 80], "z": [228, 231, 234, 237]},
}}


def _resolve_tick_override(design: str, stage: str) -> dict:
    """Return the override dict for (design, stage). Stage-specific entries
    win over a flat design-level entry; if neither matches return {}."""
    key = design[:-2] if design.endswith("_w") else design
    entry = PER_DESIGN_TICKS.get(key, {})
    if stage in entry and isinstance(entry[stage], dict):
        return entry[stage]
    if any(k in entry for k in ("x", "y", "z")):
        return entry
    return {}


def _gather_dual_series(design: str, stage: str):
    """label -> list of (util_pct, eff_cp_ps, power_mW)."""
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


def _plot_util_3d_panel(ax, design: str, stage: str,
                        title: str | None = None) -> bool:
    series = _gather_dual_series(design, stage)
    if not any(series.values()):
        return False

    baseline_label, _, baseline_color, _ = RANK_FILES[0]
    rank1_label,    _, rank1_color,    _ = RANK_FILES[1]

    all_z = []
    plotted = []
    for lbl, color in [(baseline_label, baseline_color),
                       (rank1_label,    rank1_color)]:
        pts = sorted(series.get(lbl, []), key=lambda p: p[0])
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]
        all_z.extend(zs)
        plotted.append((lbl, color, xs, ys, zs))

    if not plotted:
        return False

    z_floor = min(all_z) - (max(all_z) - min(all_z)) * 0.10
    if z_floor == min(all_z):
        z_floor = min(all_z) - 0.5

    for lbl, color, xs, ys, zs in plotted:
        ax.scatter(xs, ys, zs, color=color, s=260, marker="o",
                   edgecolors="black", linewidths=0.8,
                   label=lbl, depthshade=True, zorder=4)

    # Per-panel axis identifiers — single-letter labels in the matching colour
    # let the reader map ticks back to the legend at the top of the figure.
    al_fs = STYLE["axis_label_fontsize"] + 1
    ax.set_xlabel("x", color=AXIS_COLORS["x"],
                  fontsize=al_fs, fontweight="bold", labelpad=14)
    ax.set_ylabel("y", color=AXIS_COLORS["y"],
                  fontsize=al_fs, fontweight="bold", labelpad=22)
    ax.set_zlabel("z", color=AXIS_COLORS["z"],
                  fontsize=al_fs, fontweight="bold", labelpad=30)
    for axn, col in AXIS_COLORS.items():
        ax.tick_params(axis=axn, colors=col,
                       labelsize=STYLE["tick_fontsize"],
                       pad=AXIS_TICK_PAD[axn])
    # Match the pane-edge line of each axis to its tick colour.
    ax.xaxis.line.set_color(AXIS_COLORS["x"])
    ax.yaxis.line.set_color(AXIS_COLORS["y"])
    ax.zaxis.line.set_color(AXIS_COLORS["z"])
    if title is not None:
        ax.set_title(title, fontsize=STYLE["axis_label_fontsize"], y=0.98)

    # Slightly tilted view that shows all three axes well.
    ax.view_init(elev=22, azim=-58)
    ax.set_zlim(z_floor, max(all_z) + (max(all_z) - min(all_z)) * 0.10)

    # Exactly 4 ticks per axis, picked from a "nice" step within the range.
    xlo, xhi = ax.get_xlim()
    ylo, yhi = ax.get_ylim()
    zlo, zhi = ax.get_zlim()
    xt = _ticks_in_range(xlo, xhi, n=4, allow_decimals=False)
    yt = _ticks_in_range(ylo, yhi, n=4, allow_decimals=False)
    # Power on z may be small (e.g. gcd ~1.4 mW); allow decimals so the
    # nice-step picker actually fits 4 ticks inside the range.
    z_decimals = (zhi - zlo) < 10.0
    zt = _ticks_in_range(zlo, zhi, n=4, allow_decimals=z_decimals)
    # Per-design overrides win over the auto-picker so paper figures use the
    # tick values we've chosen by hand for consistency.
    overrides = _resolve_tick_override(design, stage)
    if "x" in overrides:
        xt = overrides["x"]
    if "y" in overrides:
        yt = overrides["y"]
    if "z" in overrides:
        zt = overrides["z"]
        z_decimals = any(abs(v - int(v)) > 1e-9 for v in zt)
    ax.xaxis.set_major_locator(FixedLocator(xt))
    ax.yaxis.set_major_locator(FixedLocator(yt))
    ax.zaxis.set_major_locator(FixedLocator(zt))
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax.zaxis.set_major_formatter(
        FormatStrFormatter("%.2f" if z_decimals else "%.0f"))
    return True


def plot_grid_util_3d(designs: list, out_path: pathlib.Path) -> bool:
    n = len(designs)
    if n == 0:
        print("  no designs provided")
        return False

    fig = plt.figure(figsize=(10.0 * n, 15))
    axes = [[None] * n for _ in range(2)]
    any_ok = False
    for col, d in enumerate(designs):
        title = _design_title(d)
        for row, stage in enumerate(("repair", "dr")):
            ax = fig.add_subplot(2, n, row * n + col + 1, projection="3d")
            axes[row][col] = ax
            ok = _plot_util_3d_panel(ax, d, stage,
                                     title=title if row == 0 else None)
            if not ok:
                print(f"  no {stage} data for '{d}'")
            any_ok = any_ok or ok

    if not any_ok:
        plt.close(fig)
        return False

    # One legend assembled from any panel that has handles.
    handles, labels = None, None
    for row in range(2):
        for col in range(n):
            h, l = axes[row][col].get_legend_handles_labels()
            if h:
                handles, labels = h, l
                break
        if handles:
            break

    # Axis-key legend laid out horizontally on the top-left, wrapped in a
    # white rounded box that matches the rank-1/baseline legend on the right.
    fs = STYLE["axis_label_fontsize"]
    axis_box = FancyBboxPatch(
        (0.005, 0.953), 0.555, 0.034,
        boxstyle="round,pad=0.002,rounding_size=0.005",
        transform=fig.transFigure,
        facecolor="white", edgecolor="0.5",
        linewidth=0.8, alpha=0.9, zorder=4,
    )
    fig.patches.append(axis_box)
    fig.text(0.015, 0.98, "x: Target util (%)",
             ha="left", va="top", fontsize=fs,
             color=AXIS_COLORS["x"], fontweight="bold", zorder=5)
    fig.text(0.205, 0.98, "y: Effective CP (ps)",
             ha="left", va="top", fontsize=fs,
             color=AXIS_COLORS["y"], fontweight="bold", zorder=5)
    fig.text(0.415, 0.98, "z: Power (mW)",
             ha="left", va="top", fontsize=fs,
             color=AXIS_COLORS["z"], fontweight="bold", zorder=5)

    # Outer figure stays the same; squeeze margins + cross-panel gaps so each
    # 3-D panel occupies more of the available area.
    fig.subplots_adjust(left=0.015, right=0.985, top=0.94, bottom=0.015,
                        hspace=-0.05, wspace=0.04)

    if handles:
        fig.legend(handles, labels,
                   loc="upper right", ncol=len(labels),
                   bbox_to_anchor=(0.995, 1),
                   frameon=True, framealpha=0.9,
                   fontsize=STYLE["legend_fontsize"])

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
                         "(default: <out-dir>/util_grid_3d.png)")
    args = ap.parse_args()

    designs = args.designs or ["aes", "gcd", "ibex", "jpeg"]
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = (pathlib.Path(args.grid_out).resolve() if args.grid_out
           else out_dir / "util_grid_3d.png")
    print(f"[util-grid / 3d / {len(designs)} designs]")
    ok = plot_grid_util_3d(designs, out)
    print(f"\n{int(ok)}/1 plots written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
