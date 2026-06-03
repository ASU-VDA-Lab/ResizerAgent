#!/usr/bin/env python3
"""Single 2-row x N-column Pareto grid for clock-sweep designs.

Top row    = repair-stage panels.
Bottom row = detail-route-stage panels.
Each panel = baseline vs Rank 1 (default; --vs-rank to swap) for one design.

Reuses data-loading helpers from plot_pareto.py — does not modify it.
"""
import argparse
import csv
import math
import pathlib
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, MaxNLocator, FormatStrFormatter

from plot_pareto import (
    CLOCK_SWEEP,
    RANK_FILES,
    STYLE,
    _parse_stage_row,
    _resolve_design,
    _variants_for,
)


# AutoTuner reference curve — third series plotted alongside Baseline + agentic.
# Source: clock_sweep/.../autotuner/<base>.csv — one flat file per base design
# (gcd/aes/jpeg/ibex), with two stage rows per clock period:
#   'autotuner_postGR' → repair row (top grid row, vs agentic after-GR repair)
#   'post_DR'          → detail-route row (bottom grid row, vs dr_post_route)
AUTOTUNER_LABEL  = "AutoTuner"
AUTOTUNER_COLOR  = "tab:green"
AUTOTUNER_MARKER = "^"
_AUTOTUNER_STAGE_ROW = {"repair": "autotuner_postGR", "dr": "post_DR"}


def _autotuner_series(csv_prefix: str, stage: str, allowed_cps=None):
    """Return sorted [(cp, eff_cp, power_W)] from autotuner/<csv_prefix>.csv for
    the requested stage. eff_cp = cp - wns (wns<0 → eff_cp>cp). When allowed_cps
    is given, keep only those CPs — used to clip the autotuner sweep to the
    agentic CP range so all three curves span the same x per panel."""
    csv_path = CLOCK_SWEEP / "autotuner" / f"{csv_prefix}.csv"
    want = _AUTOTUNER_STAGE_ROW.get(stage)
    if want is None or not csv_path.exists():
        return []
    out = []
    try:
        with csv_path.open() as fh:
            for row in csv.DictReader(fh):
                if row.get("stage") != want:
                    continue
                try:
                    cp = int(round(float(row["period_ps"])))
                    wns = float(row["wns_ps"])
                    pw = float(row["power_W"])
                except (KeyError, ValueError, TypeError):
                    continue
                if allowed_cps is not None and cp not in allowed_cps:
                    continue
                out.append((cp, cp - wns, pw))
    except OSError:
        return []
    return sorted(out, key=lambda x: x[0])


def _plot_autotuner_on_ax(ax, csv_prefix: str, stage: str, allowed_cps=None) -> bool:
    """Overlay the AutoTuner curve on a panel. No per-CP labels — baseline and
    agentic already annotate each CP, so the panel stays uncluttered."""
    pts = _autotuner_series(csv_prefix, stage, allowed_cps)
    if not pts:
        return False
    xs = [p[1] for p in pts]
    ys = [p[2] * 1000 for p in pts]
    ax.plot(xs, ys, marker=AUTOTUNER_MARKER, linestyle="-",
            color=AUTOTUNER_COLOR, label=AUTOTUNER_LABEL,
            linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
    return True


def _ticks_in_range(vmin: float, vmax: float, n: int = 4,
                    allow_decimals: bool = False) -> list:
    """Pick exactly n nicely-spaced ticks lying within [vmin, vmax].

    - Never expands the axis range.
    - Prefers the largest step that still fits n ticks, so the ticks
      spread across the visible range instead of clustering.
    - Tries both step-aligned starts and half-step-aligned starts (when
      the half-step is itself a nice integer / 1-dec value), so e.g.
      [40.2, 78.0] with step=10 can pick start=45 → [45, 55, 65, 75]
      rather than start=50 → only 3 ticks fit.
    - Among n-tick windows on a chosen step, picks the window whose
      center is closest to the data midpoint.
    """
    span = vmax - vmin
    if span <= 0:
        return [int(round(vmin))] if not allow_decimals else [round(vmin, 1)]

    mid = (vmin + vmax) / 2
    mults = (1, 2, 2.5, 5)
    exp_range = range(-3, 7) if allow_decimals else range(0, 7)

    def _ok_step(s: float) -> bool:
        if allow_decimals:
            return abs(s * 10 - round(s * 10)) < 1e-9
        return s >= 1 and abs(s - round(s)) < 1e-9

    best = None   # (score, window)  smaller score wins
    for mult in mults:
        for exp in exp_range:
            step = mult * (10 ** exp)
            if not _ok_step(step):
                continue
            if step > span:
                continue
            # Candidate start granularities: step itself, and step/2 if
            # step/2 is also a nice integer / 1-dec value.
            grans = {step}
            half = step / 2
            if _ok_step(half):
                grans.add(half)
            for gran in grans:
                start = math.ceil(vmin / gran) * gran
                all_ticks = []
                x = start
                while x <= vmax + 1e-9:
                    all_ticks.append(x)
                    x += step
                if len(all_ticks) < n:
                    continue
                # Pick the n-window whose center is closest to mid.
                best_window = None
                for i in range(len(all_ticks) - n + 1):
                    window = all_ticks[i:i + n]
                    wmid = (window[0] + window[-1]) / 2
                    dist = abs(wmid - mid)
                    if best_window is None or dist < best_window[0]:
                        best_window = (dist, window)
                # Tiebreaker: prefer step-aligned starts (gran == step) over
                # half-step starts — labels look "rounder" (e.g. 350/400/450
                # vs 375/425/475) at the same step size.
                gran_penalty = 0 if abs(gran - step) < 1e-9 else 1
                score = (-step, gran_penalty, best_window[0])
                if best is None or score < best[0]:
                    best = (score, best_window[1])

    if best is None:
        # No nice step gives n ticks in range — fall back to fewer ticks.
        for mult in mults:
            for exp in exp_range:
                step = mult * (10 ** exp)
                if not _ok_step(step) or step > span:
                    continue
                start = math.ceil(vmin / step) * step
                ticks = []
                x = start
                while x <= vmax + 1e-9:
                    ticks.append(x)
                    x += step
                if ticks:
                    if allow_decimals:
                        return [round(t, 1) for t in ticks[:n]]
                    return [int(round(t)) for t in ticks[:n]]
        return [int(round(mid))] if not allow_decimals else [round(mid, 1)]

    ticks = best[1]
    if allow_decimals:
        return [round(t, 1) for t in ticks]
    return [int(round(t)) for t in ticks]


# Per-design synthesis utilization (%) printed alongside the design name in
# the column title (e.g. "aes (Util. 65%)"). Different PDKs use different
# floorplans — keep one mapping per PDK and flip CURRENT_PDK from the
# per-PDK wrapper script to switch which one is used.
DESIGN_UTIL_PCT = {
    "asap7": {
        "gcd":  65,
        "aes":  65,
        "jpeg": 70,
        "ibex": 40,
    },
    "nangate45": {
        "gcd":  60,
        "aes":  30,
        "jpeg": 80,
        "ibex": 50,
    },
}
CURRENT_PDK = "asap7"  # wrappers (e.g. plot_pareto_grid_nangate45.py) patch this.


def _design_title(design: str) -> str:
    """Strip the optional _w suffix and append the util percentage if known."""
    base = design[:-2] if design.endswith("_w") else design
    pct = DESIGN_UTIL_PCT.get(CURRENT_PDK, {}).get(base)
    return f"{base} (Util. {pct}%)" if pct is not None else base


# Manual tick overrides for specific (design, stage, axis) cells. The
# values listed here are used verbatim as the panel's tick labels; the
# axis limits are stretched if needed so all listed ticks are visible.
# Edit / extend as needed.
CUSTOM_TICKS = {
    ("aes_w",  "repair", "x"): [274, 282, 290, 300],
    ("aes_w",  "dr",     "x"): [260, 270, 280, 295],
    ("aes_w",  "dr",     "y"): [300, 400, 500, 600],
    ("jpeg_w", "repair", "x"): [420, 460, 500, 540],
    ("jpeg_w", "repair", "y"): [220, 260, 300, 340],
    ("jpeg_w", "dr",     "y"): [220, 260, 300, 340],
}


def _best_rank_series(design: str):
    """For each CP variant of `design`, pick the rank (1..4) whose
    DR-stage WNS is best (least negative). Return two parallel series
    keyed by CP — the chosen rank's repair-stage and DR-stage points —
    plus the baseline series for both stages.

    Returns: dict with keys 'baseline_repair', 'baseline_dr',
    'best_repair', 'best_dr' each mapping to list of (cp, eff_cp, power_W).
    """
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name, csv_prefix)
    out = {"baseline_repair": [], "baseline_dr": [],
           "best_repair": [], "best_dr": []}
    if not variants:
        return out

    for cp_ps, vdir in variants:
        # Baseline (rank 0)
        b_repair_wns, b_repair_pw = _parse_stage_row(
            vdir / RANK_FILES[0][1].format(design=csv_prefix), "repair")
        b_dr_wns, b_dr_pw = _parse_stage_row(
            vdir / RANK_FILES[0][1].format(design=csv_prefix), "dr")
        if b_repair_wns is not None:
            out["baseline_repair"].append((cp_ps, cp_ps - b_repair_wns, b_repair_pw))
        if b_dr_wns is not None:
            out["baseline_dr"].append((cp_ps, cp_ps - b_dr_wns, b_dr_pw))

        # Scan ranks 1..4 for the one with best DR WNS (least negative).
        best = None  # (dr_wns, dr_pw, repair_wns, repair_pw, rank)
        for r_idx in range(1, 5):
            fname = RANK_FILES[r_idx][1].format(design=csv_prefix)
            csv_path = vdir / fname
            if not csv_path.exists():
                continue
            r_dr_wns, r_dr_pw = _parse_stage_row(csv_path, "dr")
            if r_dr_wns is None:
                continue
            r_rep_wns, r_rep_pw = _parse_stage_row(csv_path, "repair")
            cand = (r_dr_wns, r_dr_pw, r_rep_wns, r_rep_pw, r_idx)
            if best is None or r_dr_wns > best[0]:
                best = cand
        if best is None:
            continue
        dr_wns, dr_pw, rep_wns, rep_pw, _ = best
        out["best_dr"].append((cp_ps, cp_ps - dr_wns, dr_pw))
        if rep_wns is not None:
            out["best_repair"].append((cp_ps, cp_ps - rep_wns, rep_pw))
    return out


def _plot_best_of_ranks_on_ax(ax, design: str, stage: str,
                              xlim=None, ylim=None,
                              show_xlabel: bool = True,
                              show_ylabel: bool = True,
                              show_legend: bool = True,
                              title: str | None = None) -> bool:
    """Render baseline + 'best-of-4-ranks-per-CP' curve onto a given Axes."""
    series = _best_rank_series(design)
    base_key = f"baseline_{stage}"
    best_key = f"best_{stage}"
    if not series[base_key] and not series[best_key]:
        return False

    baseline_color, baseline_marker = RANK_FILES[0][2], RANK_FILES[0][3]
    best_color, best_marker = RANK_FILES[1][2], RANK_FILES[1][3]

    plot_specs = [
        ("Baseline", series[base_key], baseline_color, baseline_marker,
         dict(xytext=(25, 0),  ha="left",  va="center")),
        ("Agentic", series[best_key], best_color, best_marker,
         dict(xytext=(-25, 0), ha="right", va="center")),
    ]
    for lbl, pts, color, marker, a in plot_specs:
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[0])
        xs = [p[1] for p in pts_sorted]
        ys = [p[2] * 1000 for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        for cp, x, y in pts_sorted:
            ax.annotate(f"{cp}", (x, y * 1000), textcoords="offset points",
                        xytext=a["xytext"], ha=a["ha"], va=a["va"],
                        fontsize=STYLE["cp_label_fontsize"],
                        color=color, alpha=STYLE["cp_label_alpha"])

    if xlim is not None:
        ax.set_xlim(*xlim)
    else:
        xmin, xmax = ax.get_xlim()
        xspan = xmax - xmin
        ax.set_xlim(xmin - xspan * 0.34, xmax + xspan * 0.34)
    if ylim is not None:
        ax.set_ylim(*ylim)
    else:
        ymin, ymax = ax.get_ylim()
        yspan = ymax - ymin
        ax.set_ylim(ymin - yspan * 0.10, ymax + yspan * 0.10)

    use_decimals_y = design.startswith("gcd")
    xkey = (design, stage, "x")
    ykey = (design, stage, "y")
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    if xkey in CUSTOM_TICKS:
        x_ticks = CUSTOM_TICKS[xkey]
        ax.set_xlim(min(xmin, x_ticks[0]), max(xmax, x_ticks[-1]))
    else:
        x_ticks = _ticks_in_range(xmin, xmax, n=4, allow_decimals=False)
    if ykey in CUSTOM_TICKS:
        y_ticks = CUSTOM_TICKS[ykey]
        ax.set_ylim(min(ymin, y_ticks[0]), max(ymax, y_ticks[-1]))
    else:
        y_ticks = _ticks_in_range(ymin, ymax, n=4, allow_decimals=use_decimals_y)
    ax.xaxis.set_major_locator(FixedLocator(x_ticks))
    ax.yaxis.set_major_locator(FixedLocator(y_ticks))
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax.yaxis.set_major_formatter(
        FormatStrFormatter("%.1f" if use_decimals_y else "%.0f"))

    if show_xlabel:
        ax.set_xlabel("Effective clock period\nCP − WNS  (ps)",
                      fontsize=STYLE["axis_label_fontsize"])
    if show_ylabel:
        ax.set_ylabel("Power (mW)",
                      fontsize=STYLE["axis_label_fontsize"])
    if title is not None:
        ax.set_title(title, fontsize=STYLE["axis_label_fontsize"])
    ax.tick_params(axis="both", labelsize=STYLE["tick_fontsize"])
    ax.grid(True, alpha=0.3)
    if show_legend:
        ax.legend(loc=STYLE["legend_loc"], framealpha=0.9,
                  fontsize=STYLE["legend_fontsize"])
    return True


def plot_grid_best_of_ranks(designs: list, out_path: pathlib.Path,
                            repair_xlim=None, repair_ylim=None,
                            dr_xlim=None, dr_ylim=None) -> bool:
    """2-row x N-column figure where the agentic curve is the per-CP
    best-of-4-ranks (selection by DR WNS, repair point from the same rank).
    """
    n = len(designs)
    if n == 0:
        print("  no designs provided")
        return False

    fig, axes = plt.subplots(2, n, figsize=(7 * n, 12), squeeze=False)
    any_ok = False
    for col, d in enumerate(designs):
        ok_r = _plot_best_of_ranks_on_ax(
            axes[0, col], d, "repair",
            xlim=repair_xlim, ylim=repair_ylim,
            show_xlabel=False, show_ylabel=False, show_legend=False,
            title=_design_title(d),
        )
        ok_d = _plot_best_of_ranks_on_ax(
            axes[1, col], d, "dr",
            xlim=dr_xlim, ylim=dr_ylim,
            show_xlabel=False, show_ylabel=False, show_legend=False,
            title=None,
        )
        if not (ok_r or ok_d):
            print(f"  no data for '{d}'")
        any_ok = any_ok or ok_r or ok_d

    if not any_ok:
        plt.close(fig)
        return False

    handles, labels = None, None
    for ax in axes.flat:
        h, l = ax.get_legend_handles_labels()
        if h:
            handles, labels = h, l
            break

    fig.supxlabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    fig.supylabel("Power (mW)",
                  fontsize=STYLE["axis_label_fontsize"], x=0.005)
    fig.tight_layout()
    fig.subplots_adjust(left=0.045, right=0.985, top=0.88,
                        hspace=0.28, wspace=0.22)
    if handles:
        fig.legend(handles, labels,
                   loc="upper right", ncol=len(labels),
                   bbox_to_anchor=(0.995, 1.0),
                   frameon=True, framealpha=0.9,
                   fontsize=STYLE["legend_fontsize"])

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
    """Render baseline + one ranked Pareto curve onto a given Axes."""
    if rank not in (1, 2, 3, 4):
        return False

    chosen = [RANK_FILES[0], RANK_FILES[rank]]
    design_dir_name, csv_prefix = _resolve_design(design)
    variants = _variants_for(design_dir_name, csv_prefix)
    if not variants:
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
        return False

    # Baseline → right of marker (centered vertically);
    # Rank → left of marker (centered vertically).
    # Horizontal separation keeps the two label streams apart.
    annot_style = {
        chosen[0][0]: dict(xytext=(25, 0),  ha="left",  va="center"),
        chosen[1][0]: dict(xytext=(-25, 0), ha="right", va="center"),
    }

    for lbl, _, color, marker in chosen:
        pts = series.get(lbl, [])
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p[2])
        xs = [p[0] for p in pts_sorted]
        ys = [p[1] * 1000 for p in pts_sorted]
        ax.plot(xs, ys, marker=marker, linestyle="-", color=color, label=lbl,
                linewidth=STYLE["line_width"], markersize=STYLE["marker_size"])
        a = annot_style[lbl]
        for x, y, cp in pts_sorted:
            ax.annotate(f"{cp}", (x, y * 1000), textcoords="offset points",
                        xytext=a["xytext"], ha=a["ha"], va=a["va"],
                        fontsize=STYLE["cp_label_fontsize"],
                        color=color, alpha=STYLE["cp_label_alpha"])

    # AutoTuner reference curve, clipped to the agentic CP set so all three
    # series span the same clock periods in this panel.
    _plot_autotuner_on_ax(ax, csv_prefix, stage,
                          allowed_cps={cp for cp, _ in variants})

    # Resolve final axis limits. If the caller passed explicit xlim/ylim use
    # those verbatim; otherwise expand the autoscale range so the annotation
    # text (which sits beyond the marker by ~10 pts) stays inside the axes.
    # Labels project horizontally: baseline → right, rank1 → left.
    # Left/right pad sized to fit the offset+text; top/bottom only needs a
    # touch of slack to avoid markers hitting the axes border.
    if xlim is not None:
        ax.set_xlim(*xlim)
    else:
        xmin, xmax = ax.get_xlim()
        xspan = xmax - xmin
        ax.set_xlim(xmin - xspan * 0.34, xmax + xspan * 0.34)

    if ylim is not None:
        ax.set_ylim(*ylim)
    else:
        ymin, ymax = ax.get_ylim()
        yspan = ymax - ymin
        ax.set_ylim(ymin - yspan * 0.10, ymax + yspan * 0.10)

    # Guarantee exactly 4 ticks on every axis (custom picker — MaxNLocator
    # silently drops to fewer ticks when no "nice" step fits 4 of them).
    # Whole-number ticks for every axis except gcd's y-axis (its 1-3 mW
    # span can't hold 4 integer ticks, so 1 decimal is permitted there).
    use_decimals_y = design.startswith("gcd")
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    x_ticks = _ticks_in_range(xmin, xmax, n=4, allow_decimals=False)
    y_ticks = _ticks_in_range(ymin, ymax, n=4,
                              allow_decimals=use_decimals_y)
    ax.xaxis.set_major_locator(FixedLocator(x_ticks))
    ax.yaxis.set_major_locator(FixedLocator(y_ticks))
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax.yaxis.set_major_formatter(
        FormatStrFormatter("%.1f" if use_decimals_y else "%.0f"))
    if show_xlabel:
        ax.set_xlabel("Effective clock period\nCP − WNS  (ps)",
                      fontsize=STYLE["axis_label_fontsize"])
    if show_ylabel:
        ax.set_ylabel("Power (mW)",
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
    """2-row x N-column figure (N = len(designs))."""
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
            show_xlabel=False, show_ylabel=False,
            show_legend=False,
            title=_design_title(d),
        )
        ok_d = _plot_baseline_vs_rank_on_ax(
            ax_d, d, "dr", rank,
            xlim=dr_xlim, ylim=dr_ylim,
            show_xlabel=False, show_ylabel=False,
            show_legend=False,
            title=None,
        )
        if not (ok_r or ok_d):
            print(f"  no data for '{d}'")
        any_ok = any_ok or ok_r or ok_d

    if not any_ok:
        plt.close(fig)
        return False

    fig.supxlabel("Effective clock period = CP − WNS  (ps)",
                  fontsize=STYLE["axis_label_fontsize"])
    fig.supylabel("Power (mW)",
                  fontsize=STYLE["axis_label_fontsize"], x=0.005)

    handles, labels = None, None
    for ax in axes.flat:
        h, l = ax.get_legend_handles_labels()
        if h:
            handles, labels = h, l
            break

    fig.tight_layout()
    fig.subplots_adjust(left=0.045, right=0.985, top=0.88,
                        hspace=0.28, wspace=0.22)
    if handles:
        fig.legend(handles, labels,
                   loc="upper right", ncol=len(labels),
                   bbox_to_anchor=(0.995, 1.0),
                   frameon=True, framealpha=0.9,
                   fontsize=STYLE["legend_fontsize"])

    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  written: {out_path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--designs", nargs="*", default=None,
                    help="Designs to plot as columns (default: gcd aes jpeg ibex)")
    ap.add_argument("--vs-rank", type=int, choices=[1, 2, 3, 4], default=1,
                    help="Rank to compare against baseline (default: 1)")
    ap.add_argument("--out-dir", default=str(CLOCK_SWEEP),
                    help="Output directory for the PNG")
    ap.add_argument("--grid-out", default=None,
                    help="Explicit output PNG path "
                         "(default: <out-dir>/pareto_grid_baseline_vs_r<rank>.png)")
    ap.add_argument("--repair-xlim", nargs=2, type=float, metavar=("LEFT", "RIGHT"), default=None,
                    help="x-axis limits for the repair-stage row (ps)")
    ap.add_argument("--repair-ylim", nargs=2, type=float, metavar=("BOTTOM", "TOP"), default=None,
                    help="y-axis limits for the repair-stage row (mW)")
    ap.add_argument("--dr-xlim", nargs=2, type=float, metavar=("LEFT", "RIGHT"), default=None,
                    help="x-axis limits for the detail-route row (ps)")
    ap.add_argument("--dr-ylim", nargs=2, type=float, metavar=("BOTTOM", "TOP"), default=None,
                    help="y-axis limits for the detail-route row (mW)")
    ap.add_argument("--best-of-ranks", action="store_true",
                    help="For each CP variant, pick the rank (1..4) with the "
                         "best DR-stage WNS and use that same rank's points "
                         "for both repair and DR plots. Overrides --vs-rank.")
    args = ap.parse_args()

    designs = args.designs or ["gcd", "aes", "jpeg", "ibex"]
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.best_of_ranks:
        out = (pathlib.Path(args.grid_out).resolve() if args.grid_out
               else out_dir / "pareto_grid_baseline_vs_best_of_ranks.png")
        print(f"[grid / baseline vs best-of-ranks / {len(designs)} designs]")
        ok = plot_grid_best_of_ranks(
            designs, out,
            repair_xlim=args.repair_xlim, repair_ylim=args.repair_ylim,
            dr_xlim=args.dr_xlim, dr_ylim=args.dr_ylim,
        )
        print(f"\n{int(ok)}/1 plots written.")
        return 0

    out = (pathlib.Path(args.grid_out).resolve() if args.grid_out
           else out_dir / f"pareto_grid_baseline_vs_r{args.vs_rank}.png")
    print(f"[grid / baseline vs r{args.vs_rank} / {len(designs)} designs]")
    ok = plot_grid_baseline_vs_rank(
        designs, out, rank=args.vs_rank,
        repair_xlim=args.repair_xlim, repair_ylim=args.repair_ylim,
        dr_xlim=args.dr_xlim, dr_ylim=args.dr_ylim,
    )
    print(f"\n{int(ok)}/1 plots written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
