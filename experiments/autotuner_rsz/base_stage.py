"""
autotuner_rsz/base_stage.py — Standalone base CTS stage for the AutoTuner.

Runs `make cts` inside Docker using autotuner_rsz/ORFS_fix/, then copies the
resulting 4_1_cts.odb to autotuner_rsz/results/orfs_<orfs>/<pdk>/<design>/base/
and generates placement-parasitic artifacts via generate_base_artifacts.tcl.

Never touches run.py, work_dir/, or any agentic flow infrastructure.
"""
from __future__ import annotations

import csv
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Dict, List

DOCKER_OPENROAD = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"

_RUNTIMES_HEADER = ["stage", "runtime_s", "start_time"]


def _write_runtime_row(runtimes_csv: pathlib.Path, stage: str,
                       runtime_s: float, start_time: str) -> None:
    new_file = not runtimes_csv.exists()
    with open(runtimes_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_RUNTIMES_HEADER)
        if new_file:
            w.writeheader()
        w.writerow({"stage": stage,
                    "runtime_s": f"{runtime_s:.2f}",
                    "start_time": start_time})


def run_base_stage(
    design:        str,
    pdk:           str,
    orfs_cfg,
    pdk_cfg,
    workspace:     pathlib.Path,
    autotuner_root: pathlib.Path,
) -> pathlib.Path:
    """
    Run CTS + base artifact generation for the given design/PDK.

    Steps:
      1. docker run ... make cts  (inside autotuner_rsz/ORFS_fix/flow)
      2. copy 4_1_cts.odb + SDC → results/.../base/
      3. docker run openroad -exit generate_base_artifacts.tcl
      4. write runtimes.csv rows

    Returns path to the results base directory (contains base_cts.odb).
    """
    # ------------------------------------------------------------------ paths
    orfs_flow_dir    = workspace / orfs_cfg.root_dir_name / "flow"
    orfs_results_dir = orfs_flow_dir / "results" / pdk / design / "base"

    output_dir   = autotuner_root / "results" / f"orfs_{orfs_cfg.name}" / pdk / design
    base_dir     = output_dir / "base"
    runtimes_csv = output_dir / "runtimes.csv"
    log_dir      = base_dir   # logs live alongside base artifacts
    base_dir.mkdir(parents=True, exist_ok=True)

    docker_flow_home = f"/workspace/{orfs_cfg.root_dir_name}/flow"

    def to_docker(p: pathlib.Path) -> str:
        return "/workspace/" + p.resolve().relative_to(workspace).as_posix()

    # ------------------------------------------------------------------ step 1: make cts
    print(f"[base_stage] Running make {orfs_cfg.cts_make_target} for {design}/{pdk} ...")
    cts_log = log_dir / "base_cts.log"
    design_config = f"./designs/{pdk}/{design}/config.mk"
    make_vars = (
        f"DESIGN_CONFIG={design_config} "
        f"SKIP_CTS_REPAIR_TIMING=1 "
        f"ENABLE_POST_CTS_CLI_REPAIR_TIMING=0 "
        f"OPENROAD_EXE=/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad "
        f"YOSYS_EXE=/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"
    )
    shell_cmd = f"make clean_all {make_vars} && make {orfs_cfg.cts_make_target} {make_vars}"

    cts_start = time.time()
    cts_cmd = [
        "docker", "run", "--rm", "--user", "root",
        "-e", f"FLOW_HOME={docker_flow_home}",
        "-v", f"{workspace}:/workspace",
        "-w", docker_flow_home,
        orfs_cfg.docker_image, "/bin/sh", "-c", shell_cmd,
    ]
    print(f"[base_stage]   docker cmd: {' '.join(cts_cmd[:8])} ... {shell_cmd}")
    with open(cts_log, "w") as lf:
        lf.write(f"cmd: {' '.join(cts_cmd)}\nstarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        proc = subprocess.run(cts_cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)
        lf.write(f"\nexit_code: {proc.returncode}\n")
    cts_elapsed = time.time() - cts_start

    if proc.returncode != 0:
        raise RuntimeError(
            f"make {orfs_cfg.cts_make_target} failed (exit {proc.returncode}). "
            f"Log: {cts_log}"
        )

    # ------------------------------------------------------------------ step 2: copy ODB + SDC
    cts_odb_src = orfs_results_dir / orfs_cfg.cts_odb_name
    cts_sdc_src = orfs_results_dir / orfs_cfg.cts_sdc_name
    base_cts_odb = base_dir / "base_cts.odb"
    constraint_sdc = base_dir / "constraint.sdc"

    if not cts_odb_src.exists():
        raise FileNotFoundError(
            f"CTS ODB not found after make: {cts_odb_src}\n"
            f"Check {cts_log} for errors."
        )

    shutil.copy2(cts_odb_src, base_cts_odb)
    if cts_sdc_src.exists():
        shutil.copy2(cts_sdc_src, constraint_sdc)
    else:
        # Fallback: look for any .sdc in the results dir
        sdcs = list(orfs_results_dir.glob("*.sdc"))
        if sdcs:
            shutil.copy2(sdcs[0], constraint_sdc)
        else:
            raise FileNotFoundError(
                f"No SDC found in {orfs_results_dir}. Expected {orfs_cfg.cts_sdc_name}."
            )

    print(f"[base_stage] CTS done in {cts_elapsed:.0f}s → {base_cts_odb}")
    _write_runtime_row(runtimes_csv, "base_cts",
                       cts_elapsed, time.strftime("%Y-%m-%d %H:%M:%S"))

    # ------------------------------------------------------------------ step 3: generate_base_artifacts.tcl
    print(f"[base_stage] Running generate_base_artifacts.tcl ...")
    artifacts_log = log_dir / "base_artifacts.log"
    artifacts_tcl = autotuner_root / "scripts" / "tcl" / "generate_base_artifacts.tcl"
    if not artifacts_tcl.exists():
        raise FileNotFoundError(f"generate_base_artifacts.tcl not found: {artifacts_tcl}")

    orfs_pdk_dir = orfs_flow_dir / "platforms" / pdk
    lef_dir      = orfs_pdk_dir / "lef"
    lib_dir      = (orfs_pdk_dir / "lib" / pdk_cfg.lib_subdir
                    if pdk_cfg.lib_subdir else orfs_pdk_dir / "lib")
    setrc_tcl    = orfs_pdk_dir / pdk_cfg.setrc_tcl
    pdk_preamble = autotuner_root / pdk_cfg.preamble_tcl

    env_vars = {
        "INPUT_DB":    to_docker(base_cts_odb),
        "SDC_FILE":    to_docker(constraint_sdc),
        "OUTPUT_DIR":  to_docker(base_dir) + "/",
        "LEF_DIR":     to_docker(lef_dir) + "/",
        "LIB_DIR":     to_docker(lib_dir) + "/",
        "SETRC_TCL":   to_docker(setrc_tcl),
        "PDK_PREAMBLE": to_docker(pdk_preamble),
        "STAGE_TAG":   "base",
    }

    artifacts_cmd = ["docker", "run", "--rm", "--user", "root"]
    for k, v in env_vars.items():
        artifacts_cmd += ["-e", f"{k}={v}"]
    artifacts_cmd += ["-v", f"{workspace}:/workspace", "-w", "/workspace/experiments/autotuner_rsz"]
    artifacts_cmd += [
        orfs_cfg.docker_image,
        DOCKER_OPENROAD, "-no_init", "-exit", to_docker(artifacts_tcl),
    ]

    artifacts_start = time.time()
    with open(artifacts_log, "w") as lf:
        lf.write(f"cmd: {' '.join(artifacts_cmd)}\nstarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        proc2 = subprocess.run(artifacts_cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)
        lf.write(f"\nexit_code: {proc2.returncode}\n")
    artifacts_elapsed = time.time() - artifacts_start

    if proc2.returncode != 0:
        raise RuntimeError(
            f"generate_base_artifacts.tcl failed (exit {proc2.returncode}). "
            f"Log: {artifacts_log}"
        )

    print(f"[base_stage] Base artifacts done in {artifacts_elapsed:.0f}s → {base_dir}")
    _write_runtime_row(runtimes_csv, "base_artifacts",
                       artifacts_elapsed, time.strftime("%Y-%m-%d %H:%M:%S"))

    total_elapsed = cts_elapsed + artifacts_elapsed
    _write_runtime_row(runtimes_csv, "base_total",
                       total_elapsed, time.strftime("%Y-%m-%d %H:%M:%S"))

    # ------------------------------------------------------------------ summary
    timing_file = base_dir / "base_timing.txt"
    wns_str = "N/A"
    if timing_file.exists():
        for line in timing_file.read_text().splitlines():
            if line.startswith("WNS:"):
                wns_str = line.split(":", 1)[1].strip()
                break

    print(f"\n{'='*60}")
    print(f"[base_stage] COMPLETE — {design}/{pdk}")
    print(f"[base_stage] Total: {total_elapsed:.0f}s  "
          f"(CTS={cts_elapsed:.0f}s  artifacts={artifacts_elapsed:.0f}s)")
    print(f"[base_stage] WNS (placement parasitics): {wns_str}")
    print(f"[base_stage] base_cts.odb: {base_cts_odb}")
    print(f"[base_stage] Logs: {base_dir}/base_cts.log, base_artifacts.log")
    print(f"{'='*60}\n")

    return base_dir
