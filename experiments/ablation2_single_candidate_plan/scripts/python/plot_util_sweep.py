#!/usr/bin/env python3
"""Util-sweep plot per design: effective clock period (cp - wns) vs target util.

For each design under util_sweeps/asap7/, plot one curve per rank
(baseline, rank1/best, rank2, rank3, rank4) across all available target
utilizations.  Variant directories are expected as '<util>_<cp>' (e.g.
'40_750' = 40% target util, CP=750 ps). The CP within a design is held
fixed; the sweep is over CORE_UTILIZATION.
"""
import argparse
import csv
import pathlib
import re
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

UTIL_SWEEPS = pathlib.Path(__file__).resolve().parents[3] / "util_sweeps" / "asap7"

# Plot styling — tune here; applies to BOTH single-axis and pair plots.
STYLE = {
    "axis_label_fontsize": 30,
    "tick_fontsize":       24,
    "legend_fontsize":     24,
    "legend_loc":          "right",
    "point_label_fontsize": 20,
    "point_label_offset":   (6, 6),
    "point_label_alpha":    0.85,
    "marker_size":         14,
    "line_width":          3,
}

# rank label → CSV filename suffix, plot color, plot marker
# Distinct shapes for color-blind accessibility.
RANK_FILES = [
    ("Baseline",   "{design}_baseline.csv",   "tab:gray",   "o"),  # circle
    ("Best/Rank1", "{design}_agentic.csv",    "tab:blue",   "*"),  # star
    ("Rank2",      "{design}_agentic_r2.csv", "tab:orange", "s"),  # square
    ("Rank3",      "{design}_agentic_r3.csv", "tab:green",  "D"),  # diamond
    ("Rank4",      "{design}_agentic_r4.csv", "tab:red",    "^"),  # triangle
]


def _parse_stage_row(csv_path: pathlib.Path, stage: str):
    """Return (wns_ps, power_W) from the requested stage row, or (None, None).

    stage="dr"     → dr_post_route row
    stage="repair" → default_repair (baseline CSV) or agentic_iter*_plan* (rank CSV)
    """
    if not csv_path.exists():
        return (None, None)
    try:
        with csv_path.open() as fh:
            for row in csv.DictReader(fh):
                name = row["stage"]
                match = (
                    (stage == "dr"     and name == "dr_post_route") or
                    (stage == "repair" and (name == "default_repair"
                                            or name.startswith("agentic_iter")))
                )
                if match:
                    wns = row["wns_ps"]
                    power = row["power_W"]
                    if wns in ("N/A", "", None) or power in ("N/A", "", None):
                        return (None, None)
                    return (float(wns), float(power))
    except (OSError, ValueError, KeyError):
        return (None, None)
    return (None, None)


def _resolve_design(name: str) -> tuple:
    """Return (design_dir_name, csv_prefix).

    util_sweeps follows '<design>/<util>_<cp>/<design>_<rank>.csv'.
    Strip a trailing '_<token>' if no variant subdir matches the bare name
    (mirrors the variant-stripping in plot_pareto._resolve_design)."""
    p = UTIL_SWEEPS / name
    if not p.exists():
        return (name, name)
    if any(c.is_dir() and re.match(r"\d+_\d+$", c.name) for c in p.iterdir()):
        return (name, name)
    parts = name.rsplit("_", 1)
    if len(parts) == 2:
        return (name, parts[0])
    return (name, name)


def _variants_for(design_dir_name: str):
    """Return sorted list of (util_pct, cp_ps, variant_dir) for the given design."""
    design_dir = UTIL_SWEEPS / design_dir_name
    if not design_dir.exists():
        return []
    out = []
    for vdir in design_dir.iterdir():
        if not vdir.is_dir():
            continue
        m = re.match(r"(\d+)_(\d+)$", vdir.name)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), vdir))
    return sorted(out, key=lambda x: x[0])


_STAGE_TITLE = {
    "dr":     "detail-route stage",
    "repair": "default-repair / agentic-repair stage",
}


def _gather_series(design: str, stage: str):
    """Read all rank curves for one design. Returns (series, cp_ps, csv_prefix).

    series: rank label → list of (util_pct, eff_cp_ps, power_mW).
    cp_ps : the (single) clock period for this design (None if no variants).
    """
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name)
    if not variants:
        return defaultdict(list), None, csv_prefix

    cps_seen = {cp for _u, cp, _v in variants}
    cp_ps = next(iter(cps_seen)) if len(cps_seen) == 1 else None

    series = defaultdict(list)
    for util_pct, cp, vdir in variants:
        for label, fname_tmpl, _c, _m in RANK_FILES:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            series[label].append((util_pct, cp - wns_ps, power_w * 1000.0))
    return series, cp_ps, csv_prefix


def plot_design(design: str, out_path: pathlib.Path, stage: str,
                xlim=None, ylim=None) -> bool:
    series, cp_ps, _ = _gather_series(design, stage)
    if not any(series.values()):
        print(f"  no '{stage}' data for '{design}'")
        return False

    baseline_label, _, baseline_color, baseline_marker = RANK_FILES[0]
    rank_entries = RANK_FILES[1:]

    # Shared limits across the 4 subplots; CLI overrides take precedence.
    all_pts = [pt for pts in series.values() for pt in pts]
    xs_all = [p[0] for p in all_pts]
    ys_all = [p[1] for p in all_pts]
    pad_x = (max(xs_all) - min(xs_all)) * 0.10 or 1.0
    pad_y = (max(ys_all) - min(ys_all)) * 0.10 or 1.0
    if xlim is None:
        xlim = (min(xs_all) - pad_x, max(xs_all) + pad_x)
    if ylim is None:
        ylim = (min(ys_all) - pad_y, max(ys_all) + pad_y)

    fig, axes = plt.subplots(2, 2, figsize=(13, 10), sharex=True, sharey=True)
    cp_tag = f"CP={cp_ps} ps" if cp_ps else "CP varies"
    fig.suptitle(f"{design} — Util sweep: effective CP vs util "
                 f"({_STAGE_TITLE[stage]}, {cp_tag}, ASAP7, ORFS_fix)",
                 fontsize=13)

    for ax, (rlabel, _, rcolor, rmarker) in zip(axes.flat, rank_entries):
        for lbl, pts, color, marker in [
            (baseline_label, series.get(baseline_label, []), baseline_color, baseline_marker),
            (rlabel,         series.get(rlabel, []),         rcolor,         rmarker),
        ]:
            if not pts:
                continue
            pts_sorted = sorted(pts, key=lambda p: p[0])
            xs = [p[0] for p in pts_sorted]
            ys = [p[1] for p in pts_sorted]
            msize = 7 if marker == "o" else (12 if marker == "*" else 9)
            ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                    linewidth=1.8, markersize=msize)
            for _x, _y, pw in pts_sorted:
                ax.annotate(f"{pw:.1f}", (_x, _y), textcoords="offset points",
                            xytext=(5, 5), fontsize=7, color=color, alpha=0.75)
        ax.set_title(f"Baseline vs {rlabel}", fontsize=11)
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", framealpha=0.9, fontsize=9)

    for ax in axes[-1, :]:
        ax.set_xlabel("Target utilization (%)")
    for ax in axes[:, 0]:
        ax.set_ylabel("Effective clock period = CP − WNS  (ps)")

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def plot_design_single(design: str, out_path: pathlib.Path, stage: str,
                       xlim=None, ylim=None) -> bool:
    """Single-axis variant: baseline + all 4 ranks on one set of axes."""
    series, _cp_ps, _ = _gather_series(design, stage)
    if not any(series.values()):
        print(f"  no '{stage}' data for '{design}'")
        return False

    fig, ax = plt.subplots(figsize=(10, 7))

    for lbl, _, color, marker in RANK_FILES:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[0])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for _x, _y, pw in pts_sorted:
            ax.annotate(f"{pw:.1f}", (_x, _y), textcoords="offset points",
                        xytext=STYLE["point_label_offset"],
                        fontsize=STYLE["point_label_fontsize"],
                        color=color, alpha=STYLE["point_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_xlabel("Target utilization (%)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.set_ylabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    ax.legend(loc=STYLE["legend_loc"], framealpha=0.9,
              fontsize=STYLE["legend_fontsize"])

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def _plot_stage_on_ax(ax, design: str, stage: str,
                      xlim=None, ylim=None,
                      show_xlabel: bool = True,
                      show_ylabel: bool = True) -> bool:
    """Render one (design, stage) single-axis plot onto a given Axes.
    Returns True if any data was plotted, False otherwise."""
    series, _cp_ps, _ = _gather_series(design, stage)
    if not any(series.values()):
        return False

    for lbl, _, color, marker in RANK_FILES:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[0])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for _x, _y, pw in pts_sorted:
            ax.annotate(f"{pw:.1f}", (_x, _y), textcoords="offset points",
                        xytext=STYLE["point_label_offset"],
                        fontsize=STYLE["point_label_fontsize"],
                        color=color, alpha=STYLE["point_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    if show_xlabel:
        ax.set_xlabel("Target utilization (%)",
                      fontsize=STYLE["axis_label_fontsize"])
    if show_ylabel:
        ax.set_ylabel("Effective clock period = CP − WNS  (ps)",
                      fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    ax.legend(loc=STYLE["legend_loc"], framealpha=0.9,
              fontsize=STYLE["legend_fontsize"])
    return True


def plot_design_pair(design: str, out_path: pathlib.Path,
                     repair_xlim=None, repair_ylim=None,
                     dr_xlim=None, dr_ylim=None) -> bool:
    """Side-by-side single-axis plot for one design:
    repair stage on the left, detail-route stage on the right."""
    fig, (ax_r, ax_d) = plt.subplots(1, 2, figsize=(20, 7))
    ok_r = _plot_stage_on_ax(ax_r, design, "repair",
                             xlim=repair_xlim, ylim=repair_ylim,
                             show_xlabel=False, show_ylabel=True)
    ok_d = _plot_stage_on_ax(ax_d, design, "dr",
                             xlim=dr_xlim, ylim=dr_ylim,
                             show_xlabel=False, show_ylabel=False)
    if not (ok_r or ok_d):
        print(f"  no data for '{design}'")
        plt.close(fig)
        return False
    ax_r.set_title("Repair stage (in-loop)", fontsize=30)
    ax_d.set_title("Detail-route stage", fontsize=30)
    fig.supxlabel("Target utilization (%)",
                  fontsize=STYLE["axis_label_fontsize"])
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def _plot_baseline_vs_rank_on_ax(ax, design: str, stage: str, rank: int,
                                 xlim=None, ylim=None,
                                 show_xlabel: bool = True,
                                 show_ylabel: bool = True,
                                 show_legend: bool = True,
                                 title: str | None = None) -> bool:
    """Render baseline + one ranked curve for (design, stage) onto a given Axes.
    Returns True if any data was plotted."""
    if rank not in (1, 2, 3, 4):
        return False

    chosen = [RANK_FILES[0], RANK_FILES[rank]]
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name)
    if not variants:
        return False

    series = defaultdict(list)
    for util_pct, cp, vdir in variants:
        for label, fname_tmpl, _c, _m in chosen:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            series[label].append((util_pct, cp - wns_ps, power_w * 1000.0))

    if not any(series.values()):
        return False

    for lbl, _, color, marker in chosen:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[0])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for _x, _y, pw in pts_sorted:
            ax.annotate(f"{pw:.1f}", (_x, _y), textcoords="offset points",
                        xytext=STYLE["point_label_offset"],
                        fontsize=STYLE["point_label_fontsize"],
                        color=color, alpha=STYLE["point_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    if show_xlabel:
        ax.set_xlabel("Target utilization (%)",
                      fontsize=STYLE["axis_label_fontsize"])
    if show_ylabel:
        ax.set_ylabel("Effective clock period\nCP − WNS  (ps)",
                      fontsize=STYLE["axis_label_fontsize"])
    if title is not None:
        ax.set_title(title, fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    if show_legend:
        ax.legend(loc=STYLE["legend_loc"], framealpha=0.9,
                  fontsize=STYLE["legend_fontsize"])
    return True


def plot_grid_baseline_vs_rank(designs: list, out_path: pathlib.Path,
                               rank: int = 1,
                               repair_xlim=None, repair_ylim=None,
                               dr_xlim=None, dr_ylim=None) -> bool:
    """2-row × N-column figure (N = len(designs)):
    top row = repair-stage panels, bottom row = DR-stage panels.
    Each panel shows baseline vs the chosen rank (default Rank1) for one design."""
    n = len(designs)
    if n == 0:
        print("  no designs provided")
        return False

    fig, axes = plt.subplots(2, n, figsize=(7 * n, 12), squeeze=False)
    any_ok = False
    for col, d in enumerate(designs):
        ax_r = axes[0, col]
        ax_d = axes[1, col]
        ok_r = _plot_baseline_vs_rank_on_ax(
            ax_r, d, "repair", rank,
            xlim=repair_xlim, ylim=repair_ylim,
            show_xlabel=False, show_ylabel=(col == 0),
            show_legend=(col == n - 1),
            title=d,
        )
        ok_d = _plot_baseline_vs_rank_on_ax(
            ax_d, d, "dr", rank,
            xlim=dr_xlim, ylim=dr_ylim,
            show_xlabel=True, show_ylabel=(col == 0),
            show_legend=(col == n - 1),
            title=None,
        )
        if not (ok_r or ok_d):
            print(f"  no data for '{d}'")
        any_ok = any_ok or ok_r or ok_d

    # Row labels on the leftmost column to identify the two stages.
    axes[0, 0].annotate("Repair stage", xy=(-0.32, 0.5),
                        xycoords="axes fraction", rotation=90,
                        ha="center", va="center",
                        fontsize=STYLE["axis_label_fontsize"])
    axes[1, 0].annotate("Detail route", xy=(-0.32, 0.5),
                        xycoords="axes fraction", rotation=90,
                        ha="center", va="center",
                        fontsize=STYLE["axis_label_fontsize"])

    if not any_ok:
        plt.close(fig)
        return False

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def plot_design_vs_rank(design: str, out_path: pathlib.Path, stage: str,
                        rank: int, xlim=None, ylim=None) -> bool:
    """Single-axis plot for one design: baseline curve vs one chosen rank."""
    if rank not in (1, 2, 3, 4):
        print(f"  invalid rank {rank}; must be 1, 2, 3, or 4")
        return False

    chosen = [RANK_FILES[0], RANK_FILES[rank]]

    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name)
    if not variants:
        print(f"  no variants found for '{design}' under {UTIL_SWEEPS}")
        return False

    series = defaultdict(list)
    for util_pct, cp, vdir in variants:
        for label, fname_tmpl, _c, _m in chosen:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            series[label].append((util_pct, cp - wns_ps, power_w * 1000.0))

    if not any(series.values()):
        print(f"  no '{stage}' data for '{design}'")
        return False

    fig, ax = plt.subplots(figsize=(10, 7))

    for lbl, _, color, marker in chosen:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[0])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for _x, _y, pw in pts_sorted:
            ax.annotate(f"{pw:.1f}", (_x, _y), textcoords="offset points",
                        xytext=STYLE["point_label_offset"],
                        fontsize=STYLE["point_label_fontsize"],
                        color=color, alpha=STYLE["point_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_xlabel("Target utilization (%)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.set_ylabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    ax.legend(loc=STYLE["legend_loc"], framealpha=0.9,
              fontsize=STYLE["legend_fontsize"])

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--designs", nargs="*", default=None,
                    help="Subset of designs (default: aes ibex jpeg)")
    ap.add_argument("--stage", choices=["dr", "repair", "both"], default="both",
                    help="Stage to plot (default: both)")
    ap.add_argument("--out-dir", default=str(UTIL_SWEEPS),
                    help="Output directory for PNGs")
    ap.add_argument("--single-axis", action="store_true",
                    help="Plot baseline + all ranks on a single axes (one figure per design/stage)")
    ap.add_argument("--xlim", nargs=2, type=float, metavar=("LEFT", "RIGHT"), default=None,
                    help="Override x-axis limits (target util %), e.g. --xlim 25 80")
    ap.add_argument("--ylim", nargs=2, type=float, metavar=("BOTTOM", "TOP"), default=None,
                    help="Override y-axis limits (effective CP, ps), e.g. --ylim 750 900")
    ap.add_argument("--pair", action="store_true",
                    help="Plot repair (left) and DR (right) side by side, single-axis each, "
                         "one figure per design")
    ap.add_argument("--repair-xlim", nargs=2, type=float, metavar=("LEFT", "RIGHT"), default=None,
                    help="(pair mode) x-axis limits for the repair-stage subplot")
    ap.add_argument("--repair-ylim", nargs=2, type=float, metavar=("BOTTOM", "TOP"), default=None,
                    help="(pair mode) y-axis limits for the repair-stage subplot")
    ap.add_argument("--dr-xlim", nargs=2, type=float, metavar=("LEFT", "RIGHT"), default=None,
                    help="(pair mode) x-axis limits for the detail-route subplot")
    ap.add_argument("--dr-ylim", nargs=2, type=float, metavar=("BOTTOM", "TOP"), default=None,
                    help="(pair mode) y-axis limits for the detail-route subplot")
    ap.add_argument("--vs-rank", type=int, choices=[1, 2, 3, 4], default=None,
                    help="Plot baseline vs one specific rank (1=Best/Rank1, 2..4=Rank2..Rank4). "
                         "Single-axis, one figure per design/stage.")
    ap.add_argument("--grid", action="store_true",
                    help="Single 2xN figure: top row repair-stage, bottom row DR-stage, "
                         "one column per design. Baseline vs Rank1 (override with --vs-rank).")
    ap.add_argument("--grid-out", default=None,
                    help="(grid mode) output PNG path. Default: <out-dir>/grid_baseline_vs_r<rank>.png")
    args = ap.parse_args()

    designs = args.designs or ["aes", "ibex", "jpeg"]
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stages = ["dr", "repair"] if args.stage == "both" else [args.stage]

    if args.grid:
        rank = args.vs_rank if args.vs_rank is not None else 1
        out = (pathlib.Path(args.grid_out).resolve() if args.grid_out
               else out_dir / f"grid_baseline_vs_r{rank}.png")
        print(f"[grid / baseline vs r{rank} / {len(designs)} designs]")
        ok = plot_grid_baseline_vs_rank(
            designs, out, rank=rank,
            repair_xlim=args.repair_xlim, repair_ylim=args.repair_ylim,
            dr_xlim=args.dr_xlim, dr_ylim=args.dr_ylim,
        )
        print(f"\n{int(ok)}/1 plots written.")
        return 0

    if args.pair:
        ok = 0
        for d in designs:
            print(f"[{d} / pair]")
            out = out_dir / f"{d}_util_pair.png"
            if plot_design_pair(d, out,
                                repair_xlim=args.repair_xlim,
                                repair_ylim=args.repair_ylim,
                                dr_xlim=args.dr_xlim,
                                dr_ylim=args.dr_ylim):
                ok += 1
        print(f"\n{ok}/{len(designs)} plots written.")
        return 0

    if args.vs_rank is not None:
        ok = 0
        for stage in stages:
            suffix = "dr" if stage == "dr" else "repair"
            for d in designs:
                print(f"[{d} / {stage} / vs_r{args.vs_rank}]")
                out = out_dir / f"{d}_util_effcp_util_{suffix}_vs_r{args.vs_rank}.png"
                if plot_design_vs_rank(d, out, stage, args.vs_rank,
                                       xlim=args.xlim, ylim=args.ylim):
                    ok += 1
        print(f"\n{ok}/{len(designs) * len(stages)} plots written.")
        return 0

    plot_fn = plot_design_single if args.single_axis else plot_design
    name_suffix = "_single" if args.single_axis else ""

    ok = 0
    for stage in stages:
        suffix = "dr" if stage == "dr" else "repair"
        for d in designs:
            print(f"[{d} / {stage}{' / single' if args.single_axis else ''}]")
            out = out_dir / f"{d}_util_effcp_util_{suffix}{name_suffix}.png"
            if plot_fn(d, out, stage, xlim=args.xlim, ylim=args.ylim):
                ok += 1
    print(f"\n{ok}/{len(designs) * len(stages)} plots written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
