"""
experiments/autotuner_rsz/backend_stage.py — Backend route+finish for AutoTuner.

Reads autotuner_best.json, copies best_output.odb → openroad-flow-scripts/flow/results/
as 4_1_cts.*, then runs `make clean_route && make route finish` inside Docker.

Writes:
  - autotuner_comparison.csv  (baseline vs autotuner_best post-DR vs delta)
  - runtimes.csv updated       (backend_route_finish row)
"""
from __future__ import annotations

import csv
import json
import pathlib
import re
import subprocess
import time
from typing import Dict, List, Optional

DOCKER_OPENROAD = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"
DOCKER_YOSYS    = "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"

_RUNTIMES_HEADER = ["stage", "runtime_s", "start_time"]


def _write_runtime_row(runtimes_csv: pathlib.Path, stage: str,
                       runtime_s: float, start_time: str) -> None:
    new_file = not runtimes_csv.exists()
    with open(runtimes_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_RUNTIMES_HEADER)
        if new_file:
            w.writeheader()
        w.writerow({"stage": stage, "runtime_s": f"{runtime_s:.2f}",
                    "start_time": start_time})


def _parse_timing_file(p: pathlib.Path):
    if not p.exists():
        return None, None
    wns = tns = None
    for line in p.read_text().splitlines():
        s = line.strip()
        if s.startswith("WNS:"):
            try: wns = float(s.split(":", 1)[1])
            except ValueError: pass
        elif s.startswith("TNS:"):
            try: tns = float(s.split(":", 1)[1])
            except ValueError: pass
    return wns, tns


def _parse_area_file(p: pathlib.Path):
    if not p.exists():
        return None, None
    for line in p.read_text().splitlines():
        m = re.search(r"Design area\s+([0-9.eE+-]+)\s+um\^2\s+([0-9.]+)%", line)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def _parse_power_file(p: pathlib.Path) -> Optional[float]:
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith("Total"):
            fields = line.split()
            if len(fields) >= 5:
                try: return float(fields[4])
                except ValueError: pass
    return None


def _collect_baseline(output_dir: pathlib.Path, pdk_cfg) -> Dict:
    """Read baseline metrics: prefer default/, fall back to base/."""
    tu = pdk_cfg.time_unit_ps
    for tag, d in [("default", output_dir / "default"), ("base", output_dir / "base")]:
        wns, tns   = _parse_timing_file(d / f"{tag}_timing.txt")
        area, util = _parse_area_file(d / f"{tag}_area.rpt")
        power      = _parse_power_file(d / f"{tag}_power.rpt")
        if wns is not None:
            return {
                "wns":      wns  * tu,
                "tns":      (tns or 0.0) * tu,
                "area_um2": area  or 0.0,
                "util_pct": util  or 0.0,
                "power_W":  power or 0.0,
                "_source":  tag,
            }
    return {}


def _parse_finish_metrics(orfs_logs_dir: pathlib.Path, time_unit_ps: float) -> Optional[Dict]:
    """Parse post-DR metrics from ORFS 6_report.json + 6_report.log."""
    jpath = orfs_logs_dir / "6_report.json"
    lpath = orfs_logs_dir / "6_report.log"
    if not jpath.exists():
        return None
    try:
        d = json.loads(jpath.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    prefix = "finish__"
    wns_key = f"{prefix}timing__setup__ws"
    if wns_key not in d:
        return None

    wns_ps  = round(d[wns_key] * time_unit_ps, 3)
    tns_ps  = round(d.get(f"{prefix}timing__setup__tns", 0.0) * time_unit_ps, 3)
    area    = d.get(f"{prefix}design__instance__area__stdcell", 0.0)
    util    = d.get(f"{prefix}design__instance__utilization__stdcell", 0.0)
    power_W = d.get(f"{prefix}power__total", 0.0)

    # Override area/util from log (handles macros not in stdcell key)
    if lpath.exists():
        m = re.findall(r"Design area\s+([\d.]+)\s+um\^2\s+(\d+)%",
                       lpath.read_text(errors="ignore"))
        if m:
            area = float(m[-1][0])
            util = float(m[-1][1]) / 100.0

    return {"wns_ps":   wns_ps,
            "tns_ps":   tns_ps,
            "area_um2": round(area, 2),
            "util_pct": round(util * 100, 2),
            "power_W":  power_W}


def _write_comparison_csv(output_dir: pathlib.Path, baseline: Dict,
                           best: Dict, best_label: str) -> None:
    """Write autotuner_comparison.csv — 3 rows: baseline / best / delta."""
    path = output_dir / "autotuner_comparison.csv"

    def _f(v, fmt):
        return fmt.format(v) if isinstance(v, (int, float)) else ""

    src = baseline.get("_source", "baseline")
    base_row = [f"{src}_baseline",
                _f(baseline.get("wns"),      "{:.3f}"),
                _f(baseline.get("tns"),      "{:.3f}"),
                _f(baseline.get("area_um2"), "{:.2f}"),
                _f(baseline.get("util_pct"), "{:.2f}"),
                _f(baseline.get("power_W"),  "{:.6e}")]
    best_row = [best_label,
                _f(best.get("wns_ps"),   "{:.3f}"),
                _f(best.get("tns_ps"),   "{:.3f}"),
                _f(best.get("area_um2"), "{:.2f}"),
                _f(best.get("util_pct"), "{:.2f}"),
                _f(best.get("power_W"),  "{:.6e}")]
    try:
        delta_row = [f"delta_{best_label}_minus_{src}_baseline",
                     "{:+.3f}".format(best["wns_ps"]   - baseline["wns"]),
                     "{:+.3f}".format(best["tns_ps"]   - baseline["tns"]),
                     "{:+.2f}".format(best["area_um2"] - baseline["area_um2"]),
                     "{:+.2f}".format(best["util_pct"] - baseline["util_pct"]),
                     "{:+.6e}".format(best["power_W"]  - baseline["power_W"])]
    except (TypeError, KeyError):
        delta_row = [f"delta_{best_label}_minus_{src}_baseline",
                     "N/A", "N/A", "N/A", "N/A", "N/A"]

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["flow", "wns_ps", "tns_ps", "area_um2", "util_pct", "power_W"])
        w.writerow(base_row)
        w.writerow(best_row)
        w.writerow(delta_row)
    print(f"[backend] Comparison CSV: {path}")


def run_backend_stage(
    design:         str,
    pdk:            str,
    orfs_cfg,
    pdk_cfg,
    workspace:      pathlib.Path,
    autotuner_root: pathlib.Path,
) -> None:
    """
    Copy best_output.odb → openroad-flow-scripts/flow/results/ as 4_1_cts.* then run
    make route finish inside Docker. Writes autotuner_comparison.csv.
    """
    output_dir   = autotuner_root / "results" / pdk / design
    runtimes_csv = output_dir / "runtimes.csv"
    best_json    = output_dir / "autotuner_best.json"
    log_dir      = autotuner_root / "logs" / pdk / design
    log_dir.mkdir(parents=True, exist_ok=True)

    if not best_json.exists():
        raise FileNotFoundError(
            f"autotuner_best.json not found: {best_json}\n"
            "Run '--run-stage tune' first."
        )

    info    = json.loads(best_json.read_text())
    best_odb = pathlib.Path(info["best_odb"])
    best_sdc = output_dir / "output.sdc"
    if not best_sdc.exists():
        best_sdc = output_dir / "base" / "constraint.sdc"
    if not best_odb.exists():
        raise FileNotFoundError(f"best_output.odb not found: {best_odb}")
    if not best_sdc.exists():
        raise FileNotFoundError(f"No SDC found for backend (checked output.sdc + base/constraint.sdc)")

    orfs_flow_dir    = workspace / orfs_cfg.root_dir_name / "flow"
    orfs_results_dir = orfs_flow_dir / "results" / pdk / design / "base"
    orfs_logs_dir    = orfs_flow_dir / "logs"    / pdk / design / "base"
    docker_flow_home = f"/workspace/{orfs_cfg.root_dir_name}/flow"

    def to_docker(p: pathlib.Path) -> str:
        return "/workspace/" + p.resolve().relative_to(workspace).as_posix()

    print(f"\n{'='*70}")
    print(f"[backend] Design={design}  PDK={pdk}")
    print(f"[backend] Best trial: #{info.get('trial_idx')}  "
          f"seq=[{','.join(info.get('sequence', []))}]")
    print(f"[backend] Best ODB: {best_odb.name}")
    print(f"[backend] Best SDC: {best_sdc.name}")
    print(f"{'='*70}\n")

    # ------------------------------------------------------------------ step 1: place 4_1_cts.*
    print("[backend] Placing 4_1_cts.* in ORFS results ...")
    dst     = to_docker(orfs_results_dir)
    src_odb = to_docker(best_odb)
    src_sdc = to_docker(best_sdc)
    cp_cmd_str = (
        f"mkdir -p {dst} && "
        f"cp {src_odb} {dst}/4_1_cts.odb && "
        f"cp {src_sdc} {dst}/4_1_cts.sdc && "
        f"cp {src_odb} {dst}/4_cts.odb && "
        f"cp {src_sdc} {dst}/4_cts.sdc"
    )
    cp_cmd = ["docker", "run", "--rm", "--user", "root",
              "-v", f"{workspace}:/workspace",
              orfs_cfg.docker_image, "bash", "-c", cp_cmd_str]
    rc = subprocess.run(cp_cmd, capture_output=True).returncode
    if rc != 0:
        raise RuntimeError(f"Failed to place 4_1_cts files (exit {rc})")
    print("[backend] 4_1_cts.odb/sdc placed.")

    # ------------------------------------------------------------------ step 2: make route finish
    route_log = log_dir / "backend_route_finish.log"
    mk = (f"DESIGN_CONFIG=./designs/{pdk}/{design}/config.mk "
          f"OPENROAD_EXE={DOCKER_OPENROAD} "
          f"YOSYS_EXE={DOCKER_YOSYS} "
          f"SKIP_INCREMENTAL_REPAIR=1")

    res = f"{docker_flow_home}/results/{pdk}/{design}/base"
    lgs = f"{docker_flow_home}/logs/{pdk}/{design}/base"
    rpt = f"{docker_flow_home}/reports/{pdk}/{design}/base"
    cmd_str = (
        f"make clean_route {mk} && "
        f"rm -f {res}/6_*.odb {res}/6_*.v {res}/6_*.sdc "
        f"       {res}/6_*.def {res}/6_*.gds {res}/6_*.spef "
        f"       {lgs}/6_*.log {lgs}/6_*.json {rpt}/6_*.rpt && "
        f"make route finish {mk}"
    )
    route_cmd = [
        "docker", "run", "--rm", "--user", "root",
        "-e", f"FLOW_HOME={docker_flow_home}",
        "-v", f"{workspace}:/workspace",
        "-w", docker_flow_home,
        orfs_cfg.docker_image, "bash", "-c", cmd_str,
    ]

    print(f"[backend] Running make route finish ...")
    print(f"[backend] Log: {route_log}")
    route_start = time.time()
    with open(route_log, "w") as lf:
        lf.write(f"cmd: {' '.join(route_cmd[:8])} ... {cmd_str}\n"
                 f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        proc = subprocess.run(route_cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)
        lf.write(f"\nexit_code: {proc.returncode}\n")
    route_elapsed = time.time() - route_start

    _write_runtime_row(runtimes_csv, "backend_route_finish",
                       route_elapsed, time.strftime("%Y-%m-%d %H:%M:%S"))

    if proc.returncode != 0:
        print(f"[backend] WARNING: make route finish exited {proc.returncode} — "
              f"metrics may be incomplete. Check {route_log}")
    else:
        print(f"[backend] make route finish done in {route_elapsed:.0f}s")

    # ------------------------------------------------------------------ step 3: parse metrics
    finish_metrics = _parse_finish_metrics(orfs_logs_dir, pdk_cfg.time_unit_ps)
    if finish_metrics is None:
        print("[backend] WARNING: 6_report.json not found — final metrics unavailable")
        print(f"[backend] Check {orfs_logs_dir}/6_report.json")
        return

    print(f"[backend] Final metrics (post-DR):")
    print(f"[backend]   WNS={finish_metrics['wns_ps']:.3f}ps  "
          f"TNS={finish_metrics['tns_ps']:.3f}ps  "
          f"area={finish_metrics['area_um2']:.2f}um²  "
          f"util={finish_metrics['util_pct']:.2f}%  "
          f"power={finish_metrics['power_W']:.4e}W")

    # ------------------------------------------------------------------ step 4: comparison CSV
    baseline = _collect_baseline(output_dir, pdk_cfg)
    best_label = f"autotuner_trial_{info.get('trial_idx', 0):03d}_postDR"
    _write_comparison_csv(output_dir, baseline, finish_metrics, best_label)

    # ------------------------------------------------------------------ summary
    print(f"\n{'='*70}")
    print(f"[backend] COMPLETE — {design}/{pdk}  ({route_elapsed:.0f}s)")
    if baseline.get("wns") is not None:
        src = baseline.get("_source", "baseline")
        d_wns   = finish_metrics["wns_ps"]   - baseline["wns"]
        d_tns   = finish_metrics["tns_ps"]   - baseline["tns"]
        d_power = finish_metrics["power_W"]  - baseline.get("power_W", 0)
        print(f"[backend] vs {src} baseline:  "
              f"WNS {d_wns:+.3f}ps  TNS {d_tns:+.3f}ps  power {d_power:+.4e}W")
    print(f"[backend] Comparison: {output_dir / 'autotuner_comparison.csv'}")
    print(f"[backend] Route log:  {route_log}")
    print(f"{'='*70}\n")
