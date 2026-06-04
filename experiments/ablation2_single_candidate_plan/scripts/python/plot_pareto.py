#!/usr/bin/env python3
"""Pareto-style plot per design: effective clock period (cp - wns) vs DR power.

For each design under clock_sweep/orfs_fix/asap7/, plot one curve per rank
(baseline, rank1/best, rank2, rank3, rank4) across all available clock periods.
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

CLOCK_SWEEP = pathlib.Path(__file__).resolve().parents[3] / "clock_sweep" / "orfs_fix" / "asap7"

# Plot styling — tune here; applies to BOTH single-axis and pair plots.
STYLE = {
    "axis_label_fontsize": 30,
    "tick_fontsize":       24,
    "legend_fontsize":     24,
    "legend_loc":          "right",
    "cp_label_fontsize":   20,
    "cp_label_offset":     (6, 6),
    "cp_label_alpha":      0.85,
    "marker_size":         14,
    "line_width":          3,
}

# rank label → CSV filename suffix, plot color, plot marker
# Distinct shapes for color-blind accessibility.
RANK_FILES = [
    ("Baseline",   "{design}_baseline.csv",  "tab:gray",   "o"),  # circle
    ("Best/Rank1", "{design}_agentic.csv",   "tab:blue",   "*"),  # star
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

    Variants and CSVs use the directory name by default. If no child like
    '<name>_<int>' exists (e.g., 'jpeg_w/jpeg_330'), strip the trailing
    '_<token>' from the directory name to derive the prefix used for both
    variant subdirs and CSV filenames."""
    p = CLOCK_SWEEP / name
    if not p.exists():
        return (name, name)
    if any(c.is_dir() and re.match(rf"{re.escape(name)}_\d+$", c.name)
           for c in p.iterdir()):
        return (name, name)
    parts = name.rsplit("_", 1)
    if len(parts) == 2:
        return (name, parts[0])
    return (name, name)


def _variants_for(design_dir_name: str, csv_prefix: str):
    """Return sorted list of (cp_ps, variant_dir) for the given design."""
    design_dir = CLOCK_SWEEP / design_dir_name
    if not design_dir.exists():
        return []
    out = []
    for vdir in design_dir.iterdir():
        if not vdir.is_dir():
            continue
        m = re.match(rf"{csv_prefix}_(\d+)$", vdir.name)
        if m:
            out.append((int(m.group(1)), vdir))
    return sorted(out, key=lambda x: x[0])


_STAGE_TITLE = {
    "dr":     "detail-route stage",
    "repair": "default-repair / agentic-repair stage",
}


def plot_design(design: str, out_path: pathlib.Path, stage: str,
                xlim=None, ylim=None) -> bool:
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name, csv_prefix)
    if not variants:
        print(f"  no variants found for '{design}' under {CLOCK_SWEEP}")
        return False

    series = defaultdict(list)  # rank label → list of (eff_cp_ps, power_W, cp_ps)
    for cp_ps, vdir in variants:
        for label, fname_tmpl, _c, _m in RANK_FILES:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            eff_cp = cp_ps - wns_ps   # wns is negative → eff_cp > cp
            series[label].append((eff_cp, power_w, cp_ps))

    if not any(series.values()):
        print(f"  no '{stage}' data for '{design}'")
        return False

    baseline_label, _, baseline_color, baseline_marker = RANK_FILES[0]
    rank_entries = RANK_FILES[1:]   # the 4 ranks

    # Compute shared axis limits so all 4 subplots are directly comparable.
    # CLI overrides (xlim/ylim args) take precedence over the auto-computed range.
    all_pts = [pt for pts in series.values() for pt in pts]
    xs_all = [p[0] for p in all_pts]
    ys_all = [p[1] * 1000 for p in all_pts]
    pad_x = (max(xs_all) - min(xs_all)) * 0.08 or 1.0
    pad_y = (max(ys_all) - min(ys_all)) * 0.10 or 0.1
    if xlim is None:
        xlim = (min(xs_all) - pad_x, max(xs_all) + pad_x)
    if ylim is None:
        ylim = (min(ys_all) - pad_y, max(ys_all) + pad_y)

    fig, axes = plt.subplots(2, 2, figsize=(13, 10), sharex=True, sharey=True)
    fig.suptitle(f"{design} — Pareto: effective CP vs power "
                 f"({_STAGE_TITLE[stage]}, ASAP7, ORFS_fix)",
                 fontsize=13)

    for ax, (rlabel, _, rcolor, rmarker) in zip(axes.flat, rank_entries):
        for lbl, pts, color, marker in [
            (baseline_label, series.get(baseline_label, []), baseline_color, baseline_marker),
            (rlabel,         series.get(rlabel, []),         rcolor,         rmarker),
        ]:
            if not pts:
                continue
            pts_sorted = sorted(pts, key=lambda p: p[2])   # connect in clock-period order
            xs = [p[0] for p in pts_sorted]
            ys = [p[1] * 1000 for p in pts_sorted]
            msize = 7 if marker == "o" else (12 if marker == "*" else 9)
            ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                    linewidth=1.8, markersize=msize)
            for x, y, cp in pts_sorted:
                ax.annotate(f"{cp}", (x, y * 1000), textcoords="offset points",
                            xytext=(5, 5), fontsize=7, color=color, alpha=0.75)
        ax.set_title(f"Baseline vs {rlabel}", fontsize=11)
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", framealpha=0.9, fontsize=9)

    for ax in axes[-1, :]:
        ax.set_xlabel("Effective clock period = CP − WNS  (ps)")
    for ax in axes[:, 0]:
        ax.set_ylabel("Power (mW)")

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def plot_design_single(design: str, out_path: pathlib.Path, stage: str,
                       xlim=None, ylim=None) -> bool:
    """Single-axis variant: baseline + all 4 ranks on one set of axes."""
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name, csv_prefix)
    if not variants:
        print(f"  no variants found for '{design}' under {CLOCK_SWEEP}")
        return False

    series = defaultdict(list)
    for cp_ps, vdir in variants:
        for label, fname_tmpl, _c, _m in RANK_FILES:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            series[label].append((cp_ps - wns_ps, power_w, cp_ps))

    if not any(series.values()):
        print(f"  no '{stage}' data for '{design}'")
        return False

    fig, ax = plt.subplots(figsize=(10, 7))

    for lbl, _, color, marker in RANK_FILES:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[2])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] * 1000 for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for x, y, cp in pts_sorted:
            ax.annotate(f"{cp}", (x, y * 1000), textcoords="offset points",
                        xytext=STYLE["cp_label_offset"],
                        fontsize=STYLE["cp_label_fontsize"],
                        color=color, alpha=STYLE["cp_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_xlabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.set_ylabel("Power (mW)", fontsize=STYLE["axis_label_fontsize"])
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
    """Render one (design, stage) single-axis Pareto onto a given Axes.
    Returns True if any data was plotted, False otherwise.
    show_xlabel / show_ylabel toggle the per-axes labels (the pair plot uses
    a single figure-level x label and only the left subplot's y label)."""
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name, csv_prefix)
    if not variants:
        return False

    series = defaultdict(list)
    for cp_ps, vdir in variants:
        for label, fname_tmpl, _c, _m in RANK_FILES:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            series[label].append((cp_ps - wns_ps, power_w, cp_ps))

    if not any(series.values()):
        return False

    for lbl, _, color, marker in RANK_FILES:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[2])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] * 1000 for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for x, y, cp in pts_sorted:
            ax.annotate(f"{cp}", (x, y * 1000), textcoords="offset points",
                        xytext=STYLE["cp_label_offset"],
                        fontsize=STYLE["cp_label_fontsize"],
                        color=color, alpha=STYLE["cp_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    if show_xlabel:
        ax.set_xlabel("Effective clock period = CP − WNS  (ps)",
                      fontsize=STYLE["axis_label_fontsize"])
    if show_ylabel:
        ax.set_ylabel("Power (mW)", fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    ax.legend(loc=STYLE["legend_loc"], framealpha=0.9,
              fontsize=STYLE["legend_fontsize"])
    return True


def plot_design_pair(design: str, out_path: pathlib.Path,
                     repair_xlim=None, repair_ylim=None,
                     dr_xlim=None, dr_ylim=None) -> bool:
    """Side-by-side single-axis Pareto for one design:
    repair stage on the left, detail-route stage on the right."""
    fig, (ax_r, ax_d) = plt.subplots(1, 2, figsize=(20, 7))
    # Left (repair): keep y label, suppress per-axes x label.
    ok_r = _plot_stage_on_ax(ax_r, design, "repair",
                             xlim=repair_xlim, ylim=repair_ylim,
                             show_xlabel=False, show_ylabel=True)
    # Right (DR): suppress both y and x labels (shared x label is set below).
    ok_d = _plot_stage_on_ax(ax_d, design, "dr",
                             xlim=dr_xlim, ylim=dr_ylim,
                             show_xlabel=False, show_ylabel=False)
    if not (ok_r or ok_d):
        print(f"  no data for '{design}'")
        plt.close(fig)
        return False
    ax_r.set_title("Repair stage (in-loop)", fontsize=30)
    ax_d.set_title("Detail-route stage", fontsize=30)
    # Single x label centered under both subplots.
    fig.supxlabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def plot_design_vs_rank(design: str, out_path: pathlib.Path, stage: str,
                        rank: int, xlim=None, ylim=None) -> bool:
    """Single-axis Pareto for one design: baseline curve vs one chosen rank.
    rank ∈ {1, 2, 3, 4} selects Best/Rank1, Rank2, Rank3, or Rank4."""
    if rank not in (1, 2, 3, 4):
        print(f"  invalid rank {rank}; must be 1, 2, 3, or 4")
        return False

    chosen = [RANK_FILES[0], RANK_FILES[rank]]   # baseline + chosen rank

    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name, csv_prefix)
    if not variants:
        print(f"  no variants found for '{design}' under {CLOCK_SWEEP}")
        return False

    series = defaultdict(list)
    for cp_ps, vdir in variants:
        for label, fname_tmpl, _c, _m in chosen:
            wns_ps, power_w = _parse_stage_row(
                vdir / fname_tmpl.format(design=csv_prefix), stage)
            if wns_ps is None:
                continue
            series[label].append((cp_ps - wns_ps, power_w, cp_ps))

    if not any(series.values()):
        print(f"  no '{stage}' data for '{design}'")
        return False

    fig, ax = plt.subplots(figsize=(10, 7))

    for lbl, _, color, marker in chosen:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[2])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] * 1000 for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for x, y, cp in pts_sorted:
            ax.annotate(f"{cp}", (x, y * 1000), textcoords="offset points",
                        xytext=STYLE["cp_label_offset"],
                        fontsize=STYLE["cp_label_fontsize"],
                        color=color, alpha=STYLE["cp_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_xlabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    ax.set_ylabel("Power (mW)", fontsize=STYLE["axis_label_fontsize"])
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
                    help="Subset of designs (default: gcd aes jpeg ibex)")
    ap.add_argument("--stage", choices=["dr", "repair", "both"], default="both",
                    help="Stage to plot (default: both)")
    ap.add_argument("--out-dir", default=str(CLOCK_SWEEP),
                    help="Output directory for PNGs")
    ap.add_argument("--single-axis", action="store_true",
                    help="Plot baseline + all ranks on a single axes (one figure per design/stage)")
    ap.add_argument("--xlim", nargs=2, type=float, metavar=("LEFT", "RIGHT"), default=None,
                    help="Override x-axis limits, e.g. --xlim 420 550 (ps)")
    ap.add_argument("--ylim", nargs=2, type=float, metavar=("BOTTOM", "TOP"), default=None,
                    help="Override y-axis limits, e.g. --ylim 200 280 (mW)")
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
    args = ap.parse_args()

    designs = args.designs or ["gcd", "aes", "jpeg", "ibex"]
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stages = ["dr", "repair"] if args.stage == "both" else [args.stage]

    if args.pair:
        ok = 0
        for d in designs:
            print(f"[{d} / pair]")
            out = out_dir / f"{d}_pareto_pair.png"
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
                out = out_dir / f"{d}_pareto_effcp_power_{suffix}_vs_r{args.vs_rank}.png"
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
            out = out_dir / f"{d}_pareto_effcp_power_{suffix}{name_suffix}.png"
            if plot_fn(d, out, stage, xlim=args.xlim, ylim=args.ylim):
                ok += 1
    print(f"\n{ok}/{len(designs) * len(stages)} plots written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
