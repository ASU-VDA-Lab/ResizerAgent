#!/usr/bin/env python3
"""
run.py — Single entry point for the timing-closure agentic flow.

Handles the complete flow in one file:
  1. Generate CTS artifacts  (Docker: make clean_all + make cts-a)
  2. Prepare base / default artifacts  (Docker: OpenROAD TCL scripts)
  3. Iteration loop:
       Reporter   → builds prompt from artifacts
       Planner    → claude CLI
       Executor   → Python  (generates run_plan.tcl for the iteration's plan)
       Worker     → Docker / OpenROAD, runs the iteration's plan
       Selector   → claude CLI
       Promote    → copies the iteration's plan artifacts to iteration<N>/best/

Usage (stage-based — pick one stage per invocation, or run 'all' end-to-end):

    # Full pipeline
    python3 run.py --design aes --agent claude --pdk asap7 \\
        --run-stage all --max-iterations 6

    # Single stage
    python3 run.py --design aes --agent claude --pdk asap7 --run-stage base
    python3 run.py --design aes --agent claude --pdk asap7 --run-stage default
    python3 run.py --design aes --agent claude --pdk asap7 \\
        --run-stage LLM-iterations --max-iterations 6
    python3 run.py --design aes --agent claude --pdk asap7 \\
        --run-stage backend_default
    python3 run.py --design aes --agent claude --pdk asap7 \\
        --run-stage backend_best

    # Resume LLM-iterations from iter N (base/default already built)
    python3 run.py --design aes --agent claude --pdk asap7 \\
        --run-stage LLM-iterations --start-iteration 2

    # Clean a stage and exit
    python3 run.py --design aes --agent claude --pdk asap7 --clean default
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import json
import pathlib
import re
import xml.etree.ElementTree as ET
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Workspace layout
# ---------------------------------------------------------------------------

WORKSPACE = pathlib.Path(__file__).resolve().parent          # run.py lives at root

# ---------------------------------------------------------------------------
# ORFS configuration — pinned to the openroad-flow-scripts submodule.
# ---------------------------------------------------------------------------

@dataclass
class OrfsConfig:
    root_dir_name: str       # directory name relative to WORKSPACE
    docker_image: str        # Docker image tag
    cts_make_target: str     # make target that produces post-CTS ODB
    cts_odb_name: str        # ODB file produced by CTS stage
    cts_sdc_name: str        # SDC file produced by CTS stage

_ORFS_CFG: OrfsConfig = OrfsConfig(
    root_dir_name="openroad-flow-scripts",
    docker_image="orfs_ra",
    cts_make_target="cts",
    cts_odb_name="4_1_cts.odb",
    cts_sdc_name="4_cts.sdc",
)

SCRIPTS_PY  = WORKSPACE / "scripts" / "python"
SCRIPTS_TCL = WORKSPACE / "scripts" / "tcl"
UTILS       = SCRIPTS_PY / "utils"
WORK_DIR    = WORKSPACE / "work_dir"
ORFS_FLOW   = WORKSPACE / _ORFS_CFG.root_dir_name / "flow"

# PDK configuration — parameterized. Defaults to asap7 for backward compat.
# set_pdk() overwrites these when --pdk is passed on the CLI.
sys.path.insert(0, str(SCRIPTS_PY))
from pdk_configs import get as _get_pdk_config  # noqa: E402

_PDK_CFG   = _get_pdk_config("asap7")
PDK_NAME   = _PDK_CFG.name
ORFS_PDK   = ORFS_FLOW / "platforms" / PDK_NAME
LEF_DIR    = ORFS_PDK / "lef"
LIB_DIR    = ORFS_PDK / "lib" / _PDK_CFG.lib_subdir if _PDK_CFG.lib_subdir else ORFS_PDK / "lib"
SETRC_TCL  = ORFS_PDK / _PDK_CFG.setrc_tcl




def set_pdk(name: str) -> None:
    """Switch the global PDK. Called from main() when --pdk is parsed."""
    global _PDK_CFG, PDK_NAME, ORFS_PDK, LEF_DIR, LIB_DIR, SETRC_TCL
    _PDK_CFG  = _get_pdk_config(name)
    PDK_NAME  = _PDK_CFG.name
    ORFS_PDK  = ORFS_FLOW / "platforms" / PDK_NAME
    LEF_DIR   = ORFS_PDK / "lef"
    LIB_DIR   = ORFS_PDK / "lib" / _PDK_CFG.lib_subdir if _PDK_CFG.lib_subdir else ORFS_PDK / "lib"
    SETRC_TCL = ORFS_PDK / _PDK_CFG.setrc_tcl


# ---------------------------------------------------------------------------
# Fixed constants
# ---------------------------------------------------------------------------

DOCKER_IMAGE     = _ORFS_CFG.docker_image
DOCKER_OPENROAD  = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"
DOCKER_YOSYS     = "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"
DOCKER_FLOW_HOME = f"/workspace/{_ORFS_CFG.root_dir_name}/flow"

TCL_ERROR_PATTERNS = [
    r"undefined proc", r"wrong # args", r"invalid command name",
    r"can't read", r"no such variable", r"extra characters after close-quote",
    r"missing close-bracket", r"syntax error in expression",
    r"\[Error\] TCL", r"Error in startup script", r"wrong type",
    r"no value given for parameter", r"bad option", r"called with too",
]
OPENROAD_ERROR_PATTERNS = [
    r"\[ERROR", r"Placement failed", r"check_placement.*error",
    r"Segmentation fault", r"Aborted", r"assert.*failed", r"Cannot legalize",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def banner(title: str) -> None:
    print(f"\n{'=' * 60}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'=' * 60}", flush=True)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def to_docker(host_path: pathlib.Path) -> str:
    """Convert a host path under WORKSPACE to /workspace/... form."""
    try:
        return "/workspace/" + host_path.resolve().relative_to(WORKSPACE).as_posix()
    except ValueError:
        raise SystemExit(f"Path {host_path} is outside workspace {WORKSPACE}")


def env_to_docker(env_vars: dict) -> dict:
    """Return env_vars with WORKSPACE-relative absolute paths converted to Docker paths."""
    result = {}
    for key, val in env_vars.items():
        p = pathlib.Path(val)
        if p.is_absolute():
            try:
                result[key] = to_docker(p)
            except SystemExit:
                result[key] = val
        else:
            result[key] = val
    return result


def design_dir(agent: str, design: str) -> pathlib.Path:
    # work_dir/<agent>/orfs_<tag>/<pdk>/<design>/  — ORFS-version, PDK, and design
    # are all namespaced so old/new ORFS and asap7/nangate45 coexist without collision.
    return WORK_DIR / agent / PDK_NAME / design


def iter_dir(agent: str, design: str, n: int) -> pathlib.Path:
    return design_dir(agent, design) / "llm_iterations" / f"iteration{n}"


# ---------------------------------------------------------------------------
# Runtime tracking — one row per (iteration, stage) in runtimes.csv
# ---------------------------------------------------------------------------

_RUNTIME_FIELDS = ["iteration", "stage", "runtime_s", "started_at"]


def _runtimes_csv(agent: str, design: str) -> pathlib.Path:
    return design_dir(agent, design) / "runtimes.csv"


def _record_runtime(agent: str, design: str, iteration,
                    stage: str, runtime_s: float,
                    started_at: Optional[str] = None) -> None:
    path = _runtimes_csv(agent, design)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    row = {
        "iteration":  iteration,
        "stage":      stage,
        "runtime_s":  f"{runtime_s:.2f}",
        "started_at": started_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_RUNTIME_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


@contextlib.contextmanager
def _time_stage(agent: str, design: str, iteration, stage: str):
    """Context manager: times the block and records to runtimes.csv."""
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    t0 = time.monotonic()
    try:
        yield
    finally:
        _record_runtime(agent, design, iteration, stage,
                        time.monotonic() - t0, started_at)


# ---------------------------------------------------------------------------
# API-call tracking — one row per LLM call in apicall.csv
# ---------------------------------------------------------------------------

_APICALL_FIELDS = ["iteration", "role", "model", "api_calls"]


def _apicall_csv(agent: str, design: str) -> pathlib.Path:
    return design_dir(agent, design) / "apicall.csv"


def _count_session_api_calls(session_id: str, cwd: str = "") -> int:
    """Count assistant turns in the Claude session JSONL file.
    Searches all project dirs for the file matching session_id."""
    if not session_id:
        return 0
    try:
        projects_dir = pathlib.Path.home() / ".claude" / "projects"
        # Search all project dirs for the session file
        for project_dir in projects_dir.iterdir():
            session_file = project_dir / f"{session_id}.jsonl"
            if session_file.exists():
                count = 0
                with open(session_file) as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                            if obj.get("message", {}).get("role") == "assistant":
                                count += 1
                        except json.JSONDecodeError:
                            pass
                return count
        return 0
    except Exception:
        return 0


def _record_apicalls(agent: str, design: str, iteration, role: str,
                     model: str, api_calls: int) -> None:
    path = _apicall_csv(agent, design)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    row = {"iteration": iteration, "role": role,
           "model": model or "", "api_calls": api_calls}
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_APICALL_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


# Token tracking — one row per LLM call in tokens.csv
# ---------------------------------------------------------------------------

_TOKEN_FIELDS = ["iteration", "role", "model",
                 "input_tokens", "output_tokens",
                 "cache_creation_tokens", "cache_read_tokens",
                 "cost_usd", "duration_s", "started_at"]


def _tokens_csv(agent: str, design: str) -> pathlib.Path:
    return design_dir(agent, design) / "tokens.csv"


def _record_tokens(agent: str, design: str, iteration, role: str,
                   model: str, usage: dict, cost_usd: float,
                   duration_s: float, started_at: str) -> None:
    path = _tokens_csv(agent, design)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    row = {
        "iteration":             iteration,
        "role":                  role,
        "model":                 model or "",
        "input_tokens":          usage.get("input_tokens", 0),
        "output_tokens":         usage.get("output_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens":     usage.get("cache_read_input_tokens", 0),
        "cost_usd":              f"{cost_usd:.6f}" if cost_usd else "",
        "duration_s":            f"{duration_s:.2f}",
        "started_at":            started_at,
    }
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_TOKEN_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def _parse_claude_json_output(stdout: str) -> Optional[dict]:
    """Parse Claude CLI `-p --output-format json` output — tolerates both the
    single-object result format and line-delimited stream-json fallback."""
    s = stdout.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    last_result = None
    for line in s.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            last_result = obj
    return last_result



# ===========================================================================
# SECTION 1 — CTS generation (Docker: make cts-a)
# ===========================================================================

def run_cts(design: str, agent: str) -> None:
    """Run make clean_all + make cts-a inside Docker. Exits on failure."""
    log_path = design_dir(agent, design) / "run_logs" / "generate_cts.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    design_config = f"./designs/{PDK_NAME}/{design}/config.mk"
    make_vars = (
        f"DESIGN_CONFIG={design_config} "
        f"OPENROAD_EXE={DOCKER_OPENROAD} "
        f"YOSYS_EXE={DOCKER_YOSYS} "
        f"ENABLE_POST_CTS_CLI_REPAIR_TIMING=0 "
        f"SKIP_CTS_REPAIR_TIMING=1"
    )
    shell_cmd = f"make clean_all {make_vars} && make {_ORFS_CFG.cts_make_target} {make_vars}"

    cmd = [
        "docker", "run", "--rm", "--user", "root",
        "-e", f"FLOW_HOME={DOCKER_FLOW_HOME}",
        "-v", f"{WORKSPACE}:/workspace",
        "-w", f"/workspace/{_ORFS_CFG.root_dir_name}/flow",
        DOCKER_IMAGE,
        "/bin/bash", "-c", shell_cmd,
    ]

    log(f"CTS: running make {_ORFS_CFG.cts_make_target} for '{design}' → log: {log_path}")
    with open(log_path, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise SystemExit(f"[ERROR] make {_ORFS_CFG.cts_make_target} failed (exit {rc}). See {log_path}")

    expected = ORFS_FLOW / "results" / PDK_NAME / design / "base" / _ORFS_CFG.cts_odb_name
    if not expected.exists():
        raise SystemExit(f"[ERROR] Expected 4_1_cts.odb not found at {expected}")
    log(f"CTS: 4_1_cts.odb generated at {expected.parent}")


# ===========================================================================
# SECTION 2 — Design preparation (Docker: OpenROAD TCL)
# ===========================================================================

def _openroad_bg(tcl_script: pathlib.Path, env_vars: dict,
                  log_path: pathlib.Path, label: str) -> subprocess.Popen:
    """Launch OpenROAD in Docker as a background process. Returns Popen."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    docker_env = env_to_docker(env_vars)
    cmd = [
        "docker", "run", "--rm", "--user", "root",
        "-v", f"{WORKSPACE}:/workspace",
        "-w", "/workspace",
    ]
    for k, v in docker_env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [DOCKER_IMAGE, DOCKER_OPENROAD, "-no_init", "-exit", to_docker(tcl_script)]
    log(f"  [{label}] starting → {log_path.name}")
    fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    proc._fh = fh
    return proc


def _wait(proc: subprocess.Popen, label: str) -> int:
    rc = proc.wait()
    proc._fh.close()
    log(f"  [{label}] {'OK' if rc == 0 else f'FAILED (exit {rc})'}")
    return rc


def _base_env(base_dir: pathlib.Path) -> dict:
    return {
        "LEF_DIR":      str(LEF_DIR),
        "LIB_DIR":      str(LIB_DIR),
        "SETRC_TCL":    str(SETRC_TCL),
        "SDC_FILE":     str(base_dir / "constraint.sdc"),
        "PDK_PREAMBLE": str(WORKSPACE / _PDK_CFG.preamble_tcl),
        # Global-route signal layer range + track-capacity adjustment. Must be
        # re-applied in every standalone TCL before pin_access/global_route;
        # GRT state does not persist across write_db/read_db.
        "MIN_ROUTING_LAYER":        _PDK_CFG.min_routing_layer,
        "MAX_ROUTING_LAYER":        _PDK_CFG.max_routing_layer,
        "ROUTING_LAYER_ADJUSTMENT": str(_PDK_CFG.routing_layer_adjustment),
    }


def prepare_base_artifacts(design: str, agent: str) -> None:
    """Copy 4_1_cts.* into base/ and generate base artifacts + placement report.

    Requires make cts-a to have produced 4_1_cts.odb. Does NOT run default repair_timing.
    """
    orfs_results = ORFS_FLOW / "results" / PDK_NAME / design / "base"
    ddir         = design_dir(agent, design)
    base_dir     = ddir / "base"
    log_dir      = ddir / "run_logs"

    banner(f"PREPARE BASE — {design}")

    for d in (base_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    log("Copying CTS artifacts to base/")
    copies = [
        (orfs_results / _ORFS_CFG.cts_odb_name, base_dir / "base_cts.odb"),
        (orfs_results / _ORFS_CFG.cts_sdc_name, base_dir / "constraint.sdc"),
        (ORFS_FLOW / "designs" / PDK_NAME / design / "config.mk", base_dir / "config.mk"),
    ]
    for src, dst in copies:
        if not src.exists():
            raise SystemExit(f"[ERROR] Source not found: {src}")
        shutil.copy2(src, dst)
        log(f"  copied {src.name} → {dst}")

    log("Generating base artifacts + placement report (combined, single session)")
    env_base_art = {
        **_base_env(base_dir),
        "INPUT_DB":   str(base_dir / "base_cts.odb"),
        "OUTPUT_DIR": str(base_dir),
        "STAGE_TAG":  "base",
    }
    rc = _wait(
        _openroad_bg(SCRIPTS_TCL / "generate_base_artifacts.tcl", env_base_art,
                     log_dir / "generate_base_artifacts.log", "gen_base_artifacts"),
        "gen_base_artifacts",
    )
    if rc != 0:
        raise SystemExit("[ERROR] generate_base_artifacts.tcl failed")

    log(f"Base prepared for '{design}' at {base_dir}")


def prepare_default_artifacts(design: str, agent: str) -> None:
    """Run the single-file default stage: repair_timing + DPL →
    pre-GR save → GR → full after-GR artifacts. All inside run_default.tcl.

    Requires prepare_base_artifacts to have been run (base_cts.odb must exist).
    The saved default.odb is the PRE-GR handoff state — next iteration's seed
    and the backend's starting point. GR inside run_default.tcl runs for
    measurement only; its parasitics produce the after-GR reports but are
    not persisted to the ODB.
    """
    ddir        = design_dir(agent, design)
    base_dir    = ddir / "base"
    default_dir = ddir / "default"
    log_dir     = ddir / "run_logs"

    banner(f"PREPARE DEFAULT — {design}")

    if not (base_dir / "base_cts.odb").exists():
        raise SystemExit(f"[ERROR] base_cts.odb missing — run --run-stage base first")

    for d in (default_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    log("Running default (repair_timing → DPL → save pre-GR → GR → full metrics)")
    env_default = {
        **_base_env(base_dir),
        "INPUT_DB":   str(base_dir / "base_cts.odb"),
        "OUTPUT_DB":  str(default_dir / "default.odb"),
        "OUTPUT_DIR": str(default_dir),
        "STAGE_TAG":  "default",
    }
    rc = _wait(
        _openroad_bg(SCRIPTS_TCL / "run_default.tcl", env_default,
                     log_dir / "run_default.log", "run_default"),
        "run_default",
    )
    if rc != 0:
        raise SystemExit(f"[ERROR] run_default.tcl failed. See {log_dir}/run_default.log")

    log(f"Default prepared for '{design}' at {default_dir}")


# ===========================================================================
# SECTION 3 — Reporter (build baseline prompt)
# ===========================================================================

def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"Cannot read '{path}': {e}")


def _run_util(cmd: List[str]) -> str:
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return r.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"(extraction failed: {e.stderr[:200]})"


def _parse_clock_period(sdc: str) -> float:
    """Return the FIRST clock period (kept for backward-compat; multi-clock use _parse_clocks)."""
    m = re.search(r"create_clock\s+.*?-period\s+([0-9.]+)", sdc)
    if not m:
        raise SystemExit("create_clock -period not found in constraint.sdc")
    return float(m.group(1))


def _parse_clocks(sdc: str) -> Dict[str, float]:
    """Return {clock_name: period_ps} for all create_clock entries in the SDC."""
    clocks: Dict[str, float] = {}
    # Match: create_clock -name NAME -period P ...  (name is optional, period is required)
    for m in re.finditer(r"create_clock\s+(?:-name\s+(\S+)\s+)?.*?-period\s+([0-9.]+)", sdc):
        name = m.group(1) or f"clk_{len(clocks)+1}"
        clocks[name.strip("{}")] = float(m.group(2))
    return clocks


def _clock_of_path(section: str) -> Optional[str]:
    """Extract the clock name from a path section's 'clocked by <name>' line."""
    m = re.search(r"clocked by (\S+?)[)\s]", section)
    return m.group(1).strip() if m else None


def _per_clock_stats(sections: List[str], clocks: Dict[str, float],
                     time_unit_ps: float = 1.0) -> Dict[str, Dict]:
    """Compute per-clock WNS, worst-slack, and violator count.
    `clocks` is expected to hold periods already in ps. `time_unit_ps` normalizes
    raw slack values read from the path report to ps."""
    stats: Dict[str, Dict] = {}
    for sec in sections:
        ck = _clock_of_path(sec)
        if not ck:
            continue
        sl = _get_slack(sec)
        if sl is None:
            continue
        sl_ps = sl * time_unit_ps
        s = stats.setdefault(ck, {"wns": 0.0, "count": 0, "period": clocks.get(ck, 0.0)})
        if sl_ps < s["wns"]:
            s["wns"] = sl_ps
        if sl_ps < 0:
            s["count"] += 1
    return stats


def _parse_timing(text: str) -> Tuple[float, float]:
    wns = tns = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("WNS:"):
            wns = float(s.split(":", 1)[1])
        elif s.startswith("TNS:"):
            tns = float(s.split(":", 1)[1])
    if wns is None or tns is None:
        raise SystemExit("timing file missing WNS or TNS")
    return wns, tns


def _parse_area_util(text: str) -> Tuple[float, float]:
    m = re.search(r"Design area\s+([0-9.eE+-]+)\s+um\^2\s+([0-9.]+)%", text)
    if not m:
        raise SystemExit("area report missing 'Design area' line")
    return float(m.group(1)), float(m.group(2))


def _parse_power(text: str) -> float:
    for line in text.splitlines():
        if line.startswith("Total"):
            fields = line.split()
            if len(fields) >= 5:
                return float(fields[4])
    raise SystemExit("power report missing 'Total' line")


def _read_violations(path: pathlib.Path) -> int:
    try:
        return int(_read(path).splitlines()[0].strip())
    except (ValueError, IndexError):
        return 0


def _parse_path_sections(text: str) -> List[str]:
    sections: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if re.match(r"^Startpoint:", line) and current:
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return [s for s in sections if s.strip() and "Startpoint:" in s]


def _get_endpoint(sec: str) -> str:
    m = re.search(r"^Endpoint:\s*(.+)", sec, re.MULTILINE)
    return m.group(1).strip() if m else "unknown"


def _get_startpoint(sec: str) -> str:
    m = re.search(r"^Startpoint:\s*(.+)", sec, re.MULTILINE)
    return m.group(1).strip() if m else "unknown"


def _get_slack(sec: str) -> Optional[float]:
    # Format: "  -100.4971   slack (VIOLATED)" — number BEFORE "slack"
    m = re.search(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+slack", sec, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _build_endpoint_registry(sections: List[str], max_endpoints: int = 50) -> str:
    # ep -> {sp: worst_slack}
    pairs: Dict[str, Dict[str, float]] = {}
    for sec in sections:
        sp = _get_startpoint(sec)
        ep = _get_endpoint(sec)
        sl = _get_slack(sec)
        if sl is not None and sl < 0:
            pairs.setdefault(ep, {})[sp] = min(pairs.get(ep, {}).get(sp, 0.0), sl)

    # Sort endpoints by worst slack across all their startpoints
    ep_worst: List[Tuple[str, float, Dict[str, float]]] = []
    for ep, sp_map in pairs.items():
        worst = min(sp_map.values())
        ep_worst.append((ep, worst, sp_map))
    ep_worst.sort(key=lambda x: x[1])

    # Slack distribution histogram (buckets in ps from timing closure)
    all_slacks = [w for _, w, _ in ep_worst]
    buckets = [
        (0,   10,  "0–10 ps"),
        (10,  25,  "10–25 ps"),
        (25,  50,  "25–50 ps"),
        (50,  100, "50–100 ps"),
        (100, float("inf"), ">100 ps"),
    ]
    hist_lines = []
    for lo, hi, label in buckets:
        count = sum(1 for s in all_slacks if -hi < s <= -lo)
        if count:
            hist_lines.append(f"  {label}: {count}")
    histogram = (f"Slack distribution ({len(all_slacks)} violating endpoints):\n"
                 + "\n".join(hist_lines))

    lines = [histogram, ""]
    for ep, worst, sp_map in ep_worst[:max_endpoints]:
        ep_short = ep.split("(")[0].strip()
        sp_sorted = sorted(sp_map.items(), key=lambda x: x[1])
        sp_strs = [f"{s.split('(')[0].strip()}({sl:.1f})" for s, sl in sp_sorted[:4]]
        lines.append(f"{ep_short} | #SPs={len(sp_map)} | worst={worst:.1f} | from: {', '.join(sp_strs)}")
    return "\n".join(lines)


def _build_cell_catalog_from_paths(sections: List[str]) -> str:
    """Extract unique cell masters from path sections, look up sizes/VTs from XML catalog.
    PDK-agnostic: uses the active PDK's cell_name_regex, vt_tiers, and catalog XML."""

    catalog_path = WORKSPACE / _PDK_CFG.cell_catalog_xml
    if not catalog_path.exists():
        return f"(cell catalog XML not found at {catalog_path})"

    # Parse XML catalog: family -> {size -> [vts]}
    tree = ET.parse(str(catalog_path))
    catalog: Dict[str, Dict[str, List[str]]] = {}
    for cell_elem in tree.getroot().findall("cell"):
        name = cell_elem.get("name", "")
        catalog[name] = {}
        for size_elem in cell_elem.findall("size"):
            sname = size_elem.get("name", "")
            vts = [vt.get("name", "") for vt in size_elem.findall("vt")]
            catalog[name][sname] = sorted(vts)

    cell_re = re.compile(_PDK_CFG.cell_name_regex)
    has_vt = bool(_PDK_CFG.vt_tiers)
    seen: Dict[str, Tuple[str, str, str]] = {}  # master -> (family, size, vt)
    for sec in sections:
        for m in cell_re.finditer(sec):
            family, size, vt = m.group(1), m.group(2), (m.group(3) if m.lastindex >= 3 else "")
            # Reconstruct the master name from whatever the regex matched
            master = m.group(0)
            if master not in seen:
                seen[master] = (family, size, vt)

    if not seen:
        return "(no cells extracted from paths)"

    # Size ordering — accept asap7 (x1, xp5, x1p5, x16f) AND nangate (X1, X2, ..., X32)
    def _size_val(s: str) -> float:
        # Strip leading 'x' or 'X' prefix
        body = s[1:] if s.startswith(("x", "X")) else s
        m = re.match(r"(\d*)(?:p(\d+))?[a-z]?$", body)
        if not m:
            return 0.0
        ip = int(m.group(1)) if m.group(1) else 0
        fp = int(m.group(2)) / (10 ** len(m.group(2))) if m.group(2) else 0.0
        return ip + fp

    # Build VT-tier ordering from the PDK config (fast→slow), plus common aliases
    vt_order: Dict[str, int] = {}
    alias_map = {"SL": "SLVT", "L": "LVT", "R": "RVT", "SLVT": "SLVT", "LVT": "LVT", "RVT": "RVT"}
    for i, tier in enumerate(_PDK_CFG.vt_tiers):
        vt_order[tier] = i
        # Also accept the longer alias (e.g., SL ↔ SLVT)
        if tier in alias_map:
            vt_order[alias_map[tier]] = i

    lines = []
    families: Dict[str, List[Tuple[str, str, str]]] = {}
    for master, (family, size, vt) in sorted(seen.items()):
        families.setdefault(family, []).append((master, size, vt))

    for family in sorted(families.keys()):
        instances = families[family]
        cat_entry = catalog.get(family, {})
        all_sizes = sorted(cat_entry.keys(), key=_size_val) if cat_entry else []
        all_vts = set()
        for vt_list in cat_entry.values():
            all_vts.update(vt_list)
        all_vts_sorted = sorted(all_vts, key=lambda v: vt_order.get(v, 99))
        # Display VTs in short form (SLVT→SL) when the PDK uses short VT tier names
        vt_display_map = {v: k for k, v in alias_map.items() if k in _PDK_CFG.vt_tiers and k != v}
        vts_display = [vt_display_map.get(v, v) for v in all_vts_sorted]

        lines.append(f"{family}:")
        lines.append(f"  Available sizes: {', '.join(all_sizes) if all_sizes else 'NONE (unsizable)'}")
        if has_vt:
            lines.append(f"  Available VTs:   {', '.join(vts_display) if vts_display else 'unknown'}")
        # Per-instance annotations
        for master, size, vt in instances:
            can_sizeup = False
            sizeup_target = None
            if all_sizes:
                idx = next((i for i, s in enumerate(all_sizes) if s == size), -1)
                if idx >= 0 and idx < len(all_sizes) - 1:
                    can_sizeup = True
                    sizeup_target = all_sizes[idx + 1]
            sizeup_note = f"→ sizeup to {sizeup_target}" if can_sizeup else "(max size or unsizable)"
            vt_note = ""
            if has_vt and vt and _PDK_CFG.vt_tiers:
                # Is there a faster VT tier available for this cell?
                fastest = _PDK_CFG.vt_tiers[0]
                if vt != fastest and (fastest in vts_display or alias_map.get(fastest, "") in all_vts):
                    vt_note = f" | vt_swap {vt}→{fastest} possible"
            suffix = f" vt={vt}" if has_vt and vt else ""
            lines.append(f"    {master}: size={size}{suffix}  {sizeup_note}{vt_note}")
        lines.append("")

    return "\n".join(lines)


def _build_prompt(
    design: str,
    b_wns: float, b_tns: float, b_area: float, b_util: float, b_power: float, b_viol: int,
    d_wns: float, d_tns: float, d_area: float, d_util: float, d_power: float, d_viol: int,
    path_count: int, path_summary: str, endpoint_registry: str,
    neighbor_netlist: str, cell_catalog: str, placement_report: str = "",
    clock_summary: str = "",
    pdk_constraints: str = "",
    gr_summary: str = "",
) -> str:
    return f"""Design {design} — Reporter Data

Clock domains (multi-clock designs: WNS/severity are always relative to the path's OWN clock period, not the first one):
{clock_summary}

Target: WNS ≥ 0 on every clock domain

{pdk_constraints}

Base metrics (post-CTS, no repair):
Design {design}: WNS={b_wns:.3f} ps, TNS={b_tns:.3f} ps, \
Area={b_area:.0f} um^2 ({b_util:.0f}% util), Power={b_power:.4e} W, \
Violating endpoints={b_viol}

Default repair_timing metrics (reference baseline — bar to beat):
Design {design}: WNS={d_wns:.3f} ps, TNS={d_tns:.3f} ps, \
Area={d_area:.0f} um^2 ({d_util:.0f}% util), Power={d_power:.4e} W, \
Violating endpoints={d_viol}

Default vs Base delta: ΔWNS={d_wns - b_wns:+.1f} ps, ΔTNS={d_tns - b_tns:+.1f} ps, \
ΔArea={d_area - b_area:+.0f} um^2 ({d_util - b_util:+.0f}% util), \
ΔViolations={d_viol - b_viol:+d}

{gr_summary if gr_summary.strip() else ""}
Worst-path info (base database, pre-repair):

Number of worst paths: {path_count} (up to 25 unique endpoints, 5 paths per endpoint)
Top worst paths summary (columns: inst/pin  master  cell_dly_ps  net_dly_ps  slew_ps  cap_fF  net):
  cell_dly_ps = intrinsic gate delay; net_dly_ps = wire RC delay to next stage input;
  slew_ps = output transition time; cap_fF = output load capacitance; net = connecting net name.
{path_summary}

Startpoint → Endpoint violation registry (unique violating setup pairs, worst slack first):
{endpoint_registry}

1-hop netlist neighborhood of top 10 unique worst-endpoint paths:
{neighbor_netlist}

Cell catalog for worst-path cells:
{cell_catalog}

Placement and floorplan context:
{placement_report if placement_report.strip() else "(placement report not available)"}""".strip()


def _build_compact_neighbors(sections: List[str], netlist_file: pathlib.Path,
                             top_n: int = 10) -> str:
    """Build compact driver/fanout table for top N unique-endpoint worst paths.

    For each critical-path cell: shows its instance, master, driver cells,
    output net name, and fanout sink count. Skips clock tree cells.
    """
    if not netlist_file.exists():
        return "(netlist file not found)"

    # Parse netlist: build inst_name -> {cell, pins: {pin_name: net_name}}
    netlist_text = netlist_file.read_text(errors="replace")
    # Match: CellMaster InstName (.PinA(NetA), .PinB(NetB), ...);
    inst_re = re.compile(
        r"^\s*([A-Za-z0-9_]+)\s+(\\?[A-Za-z0-9_./\[\]$]+)\s*\(([^;]*)\);",
        re.MULTILINE | re.DOTALL,
    )
    # Pin connections: .PinName(NetName)
    pin_re = re.compile(r"\.(\w+)\(([^)]+)\)")

    # Build: inst_name -> {cell, pins: {pin: net}}
    # Build: net_name -> {drivers: [inst], sinks: [inst]}
    inst_info: Dict[str, Dict] = {}
    net_drivers: Dict[str, List[str]] = {}
    net_sinks: Dict[str, List[str]] = {}

    for m in inst_re.finditer(netlist_text):
        cell, inst_raw, pin_block = m.group(1), m.group(2), m.group(3)
        inst = inst_raw.lstrip("\\").strip()
        pins = {}
        for pm in pin_re.finditer(pin_block):
            pins[pm.group(1)] = pm.group(2).strip()
        inst_info[inst] = {"cell": cell, "pins": pins}

        # Determine driver/sink by pin name convention
        # Output pins: Y, Q, QN, SN, CON, SUM, COUT, S, CO, Z
        out_pins = {"Y", "Q", "QN", "SN", "CON", "SUM", "COUT", "S", "CO", "Z"}
        for pname, net in pins.items():
            if pname in out_pins:
                net_drivers.setdefault(net, []).append(inst)
            elif pname not in ("VDD", "VSS"):
                net_sinks.setdefault(net, []).append(inst)

    # Extract path cell instances from timing report sections
    path_cell_re = re.compile(r"[\^v]\s+([A-Za-z0-9_.$\[\]\\]+)/[A-Za-z_]\w*")
    clock_skip = re.compile(r"^clkbuf_|^clknet_|^clkgate_|^clkinv_|^clk_|^cts_|^CK", re.IGNORECASE)

    # Get top N unique endpoints
    seen_eps: Dict[str, int] = {}
    for i, sec in enumerate(sections):
        ep = _get_endpoint(sec)
        if ep not in seen_eps:
            seen_eps[ep] = i
        if len(seen_eps) >= top_n:
            break

    lines = []
    out_pins = {"Y", "Q", "QN", "SN", "CON", "SUM", "COUT", "S", "CO", "Z"}

    for ep, sec_idx in seen_eps.items():
        sec = sections[sec_idx]
        slack = _get_slack(sec) or 0.0

        # Extract instances on this path
        path_insts = []
        for m in path_cell_re.finditer(sec):
            inst = m.group(1).lstrip("\\")
            if clock_skip.match(inst):
                continue
            if inst not in path_insts:
                path_insts.append(inst)

        if not path_insts:
            continue

        ep_short = ep.split("(")[0].strip()[:40]
        lines.append(f"Path {sec_idx+1} (slack={slack:.1f}, endpoint={ep_short}):")

        for inst in path_insts:
            info = inst_info.get(inst)
            if not info:
                continue

            cell = info["cell"]
            pins = info["pins"]

            # Input drivers: pin←net(driver)
            # Skip clock pins and any driver that is a clock tree cell.
            clock_pins = {"CLK", "CK", "CLOCK", "clk", "ck"}
            input_parts = []
            for pname, net in sorted(pins.items()):
                if pname in out_pins or pname in ("VDD", "VSS") or pname in clock_pins:
                    continue
                drivers = net_drivers.get(net, [])
                # Filter clock tree instances from driver display
                data_drivers = [d for d in drivers if not clock_skip.match(d)]
                if not data_drivers and drivers:
                    continue  # all drivers are clock tree — skip this pin entirely
                drv = data_drivers[0] if len(data_drivers) == 1 else f"{len(data_drivers)}drv"
                input_parts.append(f"{pname}←{net}({drv})")

            # Output fanout: pin→net(N: sink1,sink2,...)
            output_parts = []
            for pname, net in sorted(pins.items()):
                if pname in out_pins:
                    sinks = [s for s in net_sinks.get(net, []) if s != inst]
                    sink_str = ",".join(sinks[:4])
                    if len(sinks) > 4:
                        sink_str += ",..."
                    output_parts.append(f"{pname}→{net}({len(sinks)}:{sink_str})")

            in_str = " ".join(input_parts[:4]) if input_parts else "—"
            out_str = " ".join(output_parts) if output_parts else "—"
            lines.append(f"  {cell} {inst} | in: {in_str} | out: {out_str}")

        lines.append("")

    return "\n".join(lines) if lines else "(no neighbor data extracted)"


def run_reporter(agent: str, design: str, iterN: int,
                 path_limit: int = 25, neighbor_paths: int = 10,
                 backtrack_from: Optional[int] = None) -> bool:
    """Build the reporter baseline prompt for iteration<N>. Returns True on success.
    If backtrack_from is set, seed from that iteration's best ODB instead of N-1."""
    if backtrack_from:
        log(f"Reporter: building prompt for Iteration {iterN} (BACKTRACK seed from Iteration {backtrack_from})")
    else:
        log(f"Reporter: building prompt for Iteration {iterN}")

    ddir        = design_dir(agent, design)
    base_dir    = ddir / "base"
    default_dir = ddir / "default"
    idir        = iter_dir(agent, design, iterN)
    start_dir   = idir / "start"

    for d in (base_dir, default_dir):
        if not d.is_dir():
            log(f"[ERROR] Directory not found: {d}")
            return False

    idir.mkdir(parents=True, exist_ok=True)
    start_dir.mkdir(parents=True, exist_ok=True)

    # Populate start/ — seed ODB + artifacts
    if iterN == 1:
        seed_odb = base_dir / "base_cts.odb"
        artifact_copies = {
            base_dir / "base_metrics.csv":       start_dir / "seed_metrics.csv",
            base_dir / "base_timing.txt":        start_dir / "seed_timing.txt",
            base_dir / "base_worst_paths.txt":   start_dir / "seed_worst_paths.txt",
            base_dir / "base_area.rpt":          start_dir / "seed_area.rpt",
            base_dir / "base_power.rpt":         start_dir / "seed_power.rpt",
            base_dir / "violating_endpoint.txt": start_dir / "violating_endpoint.txt",
        }
    else:
        source_iter = backtrack_from if backtrack_from else (iterN - 1)
        prev_best = iter_dir(agent, design, source_iter) / "best"
        seed_odb  = prev_best / "output.odb"
        artifact_copies = {
            prev_best / "best_metrics.csv":          start_dir / "seed_metrics.csv",
            prev_best / "best_timing.txt":           start_dir / "seed_timing.txt",
            prev_best / "best_worst_paths.txt":      start_dir / "seed_worst_paths.txt",
            prev_best / "best_area.rpt":             start_dir / "seed_area.rpt",
            prev_best / "best_power.rpt":            start_dir / "seed_power.rpt",
            prev_best / "violating_endpoint.txt":    start_dir / "violating_endpoint.txt",
            prev_best / "best_placement_report.txt": start_dir / "seed_placement_report.txt",
            prev_best / "best_netlist.v":            start_dir / "seed_netlist.v",
        }

    if not seed_odb.exists():
        log(f"[ERROR] Seed ODB not found: {seed_odb}")
        return False

    shutil.copy2(seed_odb, start_dir / "seed.odb")
    for src, dst in artifact_copies.items():
        if src.exists():
            shutil.copy2(src, dst)

    # --- Parse base + default metrics (always needed for reference) ---
    log("[1/7] Parsing base + default metrics")
    sdc_text = _read(base_dir / "constraint.sdc")
    # Normalize time values to ps (asap7 is already ps; nangate45 is ns → ×1000)
    tu = _PDK_CFG.time_unit_ps
    clocks   = {k: v * tu for k, v in _parse_clocks(sdc_text).items()}
    period   = _parse_clock_period(sdc_text) * tu
    _bw, _bt = _parse_timing(_read(base_dir / "base_timing.txt"))
    b_wns, b_tns = _bw * tu, _bt * tu
    b_area, b_util = _parse_area_util(_read(base_dir / "base_area.rpt"))
    b_power  = _parse_power(_read(base_dir / "base_power.rpt"))
    b_viol   = _read_violations(base_dir / "violating_endpoint.txt")
    _dw, _dt = _parse_timing(_read(default_dir / "default_timing.txt"))
    d_wns, d_tns = _dw * tu, _dt * tu
    d_area, d_util = _parse_area_util(_read(default_dir / "default_area.rpt"))
    d_power  = _parse_power(_read(default_dir / "default_power.rpt"))
    d_viol   = _read_violations(default_dir / "violating_endpoint.txt")

    # --- Choose analysis source: base for iter 1, seed for iter N≥2 ---
    if iterN == 1:
        worst_path_file = base_dir / "base_worst_paths.txt"
        netlist_file    = base_dir / "base_netlist.v"
        plc_file        = base_dir / "base_placement_report.txt"
    else:
        worst_path_file = start_dir / "seed_worst_paths.txt"
        netlist_file    = start_dir / "seed_netlist.v"
        plc_file        = start_dir / "seed_placement_report.txt"

    # --- Run analysis pipeline on the current source ---
    log(f"[2/7] Extracting path summary from {'base' if iterN == 1 else 'seed'}")
    path_summary = _run_util(["python3", str(UTILS / "extract_max_paths.py"),
                               str(worst_path_file), "--limit", str(path_limit)])

    log("[3/7] Building endpoint registry")
    sections   = _parse_path_sections(_read(worst_path_file))
    path_count = len(sections)
    endpoint_registry = _build_endpoint_registry(sections)

    # Per-clock analysis — critical for multi-clock designs (slack values normalized to ps)
    per_clock = _per_clock_stats(sections, clocks, time_unit_ps=tu)
    # Determine the worst-path clock (tightest % violation)
    worst_clock_name = None
    worst_wns = 0.0
    for ck, s in per_clock.items():
        if s["wns"] < worst_wns:
            worst_wns = s["wns"]
            worst_clock_name = ck
    # Build per-clock summary string
    clock_lines = []
    for ck, per in sorted(clocks.items(), key=lambda x: x[1]):
        s = per_clock.get(ck, {"wns": 0.0, "count": 0})
        severity = (abs(s['wns']) / per * 100) if per > 0 and s['wns'] < 0 else 0.0
        marker = " ← worst-path clock" if ck == worst_clock_name else ""
        clock_lines.append(
            f"  {ck}: period={per:.0f}ps  WNS={s['wns']:.2f}ps  "
            f"violators={s['count']}  severity={severity:.1f}%{marker}"
        )
    clock_summary = "\n".join(clock_lines) if clock_lines else "(no clock info)"

    # PDK-specific constraints the Planner must respect
    if not _PDK_CFG.vt_tiers:
        pdk_constraints = (
            f"PDK-SPECIFIC CONSTRAINTS ({PDK_NAME}, single-VT library):\n"
            f"  - vt_swap is a NO-OP. Do NOT include vt_swap in any sequence or staged plan.\n"
            f"    The Liberty library has only one VT tier, so OpenROAD silently skips vt_swap\n"
            f"    without changing any cell. Including it wastes a slot and gives false signal.\n"
            f"  - skip_crit_vt_swap knob has no effect — do not set it; it's handled internally.\n"
            f"  - ECO actions must NOT use VT suffixes in new_master (e.g., use INV_X4, not INV_LVT_X4).\n"
            f"  - Primary levers on this PDK: sizeup, sizeup_match, buffer, clone, split, swap, unbuffer."
        )
    else:
        pdk_constraints = (
            f"PDK-SPECIFIC CONSTRAINTS ({PDK_NAME}, multi-VT library with tiers {_PDK_CFG.vt_tiers}):\n"
            f"  - vt_swap is available and moves cells toward the faster tier (e.g. R→L→SL for asap7).\n"
            f"  - A cell already at the fastest VT ({_PDK_CFG.vt_tiers[0]}) cannot be swapped further."
        )

    log(f"[4/7] Building compact neighbor tables for top {neighbor_paths} endpoints")
    neighbor_netlist = _build_compact_neighbors(sections, netlist_file, top_n=neighbor_paths)

    log("[5/7] Building cell catalog from XML")
    cell_catalog = _build_cell_catalog_from_paths(sections)

    placement_report = _read(plc_file) if plc_file.exists() else ""
    log(f"[6/7] Placement report: {'found' if plc_file.exists() else 'not found'}")

    # --- Parse GR metrics from the relevant log (iter1: default; N≥2: seed plan log) ---
    gr_summary = ""
    try:
        sys.path.insert(0, str(SCRIPTS_PY))
        from parse_gr_metrics import parse_gr_metrics, format_gr_summary  # type: ignore
        if iterN == 1:
            gr_log = ddir / "run_logs" / "run_default.log"
            gr_label = "Default-stage global route (post-DPL handoff measurement):"
        else:
            source_iter = backtrack_from if backtrack_from else (iterN - 1)
            best_plan_f = iter_dir(agent, design, source_iter) / "best" / "best_plan.txt"
            seed_plan = None
            if best_plan_f.exists():
                for ln in best_plan_f.read_text().splitlines():
                    if ln.startswith("plan="):
                        seed_plan = ln.split("=", 1)[1].strip()
                        break
            gr_log = (iter_dir(agent, design, source_iter) / seed_plan / f"{seed_plan}.log") \
                     if seed_plan else None
            gr_label = (f"Seed plan ({seed_plan}) global route from Iteration {source_iter}:"
                        if seed_plan else "")
        if gr_log and gr_log.exists():
            metrics = parse_gr_metrics(gr_log)
            if metrics:
                gr_summary = f"{gr_label}\n{format_gr_summary(metrics)}\n"
                log(f"  GR summary: {len(metrics)} fields parsed from {gr_log.name}")
            else:
                log(f"  GR summary: no GRT-* lines in {gr_log.name}")
        else:
            log(f"  GR summary: log not found ({gr_log})")
    except Exception as e:  # noqa: BLE001
        log(f"  GR summary: skipped ({e})")

    # --- Assemble prompt ---
    log("[7/7] Assembling prompt")

    if iterN == 1:
        # Iteration 1: full base analysis
        prompt = _build_prompt(
            design=design,
            b_wns=b_wns, b_tns=b_tns, b_area=b_area, b_util=b_util,
            b_power=b_power, b_viol=b_viol,
            d_wns=d_wns, d_tns=d_tns, d_area=d_area, d_util=d_util,
            d_power=d_power, d_viol=d_viol,
            path_count=path_count, path_summary=path_summary,
            endpoint_registry=endpoint_registry, neighbor_netlist=neighbor_netlist,
            cell_catalog=cell_catalog, placement_report=placement_report,
            clock_summary=clock_summary,
            pdk_constraints=pdk_constraints,
            gr_summary=gr_summary,
        )
    else:
        # N≥2: compact base/default summary + full seed analysis + comparison
        _sw, _st       = _parse_timing(_read(start_dir / "seed_timing.txt"))
        s_wns, s_tns   = _sw * tu, _st * tu
        s_area, s_util = _parse_area_util(_read(start_dir / "seed_area.rpt"))
        s_power        = _parse_power(_read(start_dir / "seed_power.rpt"))
        s_viol         = _read_violations(start_dir / "violating_endpoint.txt")

        # Build iteration history — compact summary + detailed last 3 iterations
        llm_root = design_dir(agent, design) / "llm_iterations"

        # Part A: compact WNS trajectory across all prior iterations
        summary_rows: List[str] = []
        for k in range(1, iterN):
            best_plan_f = llm_root / f"iteration{k}" / "best" / "best_plan.txt"
            best_name = best_wns = best_tns = "?"
            if best_plan_f.exists():
                for line in best_plan_f.read_text().splitlines():
                    if line.startswith("plan="): best_name = line.split("=", 1)[1].strip()
                    elif line.startswith("wns="): best_wns = line.split("=", 1)[1].strip()
                    elif line.startswith("tns="): best_tns = line.split("=", 1)[1].strip()
            summary_rows.append(f"  Iter{k}: plan={best_name} WNS={best_wns} TNS={best_tns}")
        history_str = "\n".join(summary_rows) or "  (none)"

        # Part B: detailed per-plan dump for the last 3 iterations
        def _plan_move_summary(pd: dict) -> Dict[str, str]:
            """plan name -> short move/sequence description."""
            out: Dict[str, str] = {}
            for i, p in enumerate(pd.get("plans", []), 1):
                pt = p.get("plan_type", "?")
                name = f"plan{i}"
                if pt == "sequence":
                    seq = ",".join(p.get("sequence", []))
                    knobs = p.get("run_knobs", {})
                    knob_str = " ".join(f"{k}={v}" for k, v in knobs.items()) if knobs else ""
                    out[name] = f"seq[{seq}] {knob_str}".strip()
                elif pt == "staged":
                    moves = "→".join(s.get("move", "?") for s in p.get("stages", []))
                    out[name] = f"staged[{moves}]"
                elif pt == "eco":
                    changes = p.get("changes", [])
                    out[name] = f"eco[{len(changes)} changes]"
                else:
                    out[name] = pt
            return out

        detail_rows: List[str] = []
        start_k = max(1, iterN - 3)
        for k in range(start_k, iterN):
            iter_k = llm_root / f"iteration{k}"
            planner_k = iter_k / "planner_decision.json"
            best_plan_f = iter_k / "best" / "best_plan.txt"
            chosen_plan_name = "?"
            if best_plan_f.exists():
                for line in best_plan_f.read_text().splitlines():
                    if line.startswith("plan="):
                        chosen_plan_name = line.split("=", 1)[1].strip()
                        break
            _ = chosen_plan_name  # captured for debugging; not consumed downstream

            # Seed WNS for iter k = the iteration's WNS from iter k-1 (or default if k=1)
            seed_wns: Optional[float] = None
            if k == 1:
                dm = default_dir / "default_metrics.csv"
                if dm.exists():
                    with open(dm, newline="") as f:
                        for row in csv.DictReader(f):
                            try: seed_wns = float(row.get("wns") or row.get("wns_ps", 0))
                            except (ValueError, TypeError): pass
                            break
            else:
                prev_best = llm_root / f"iteration{k-1}" / "best" / "best_plan.txt"
                if prev_best.exists():
                    for line in prev_best.read_text().splitlines():
                        if line.startswith("wns="):
                            try: seed_wns = float(line.split("=", 1)[1].strip())
                            except (ValueError, TypeError): pass

            move_map: Dict[str, str] = {}
            if planner_k.exists():
                try:
                    pd_json = json.loads(planner_k.read_text())
                    move_map = _plan_move_summary(pd_json)
                except (json.JSONDecodeError, OSError):
                    pass

            # Read all plan_metrics.csv (survives retries, unlike iteration_summary.csv)
            plan_results = []
            for plan_dir in sorted(iter_k.glob("plan*")):
                if not plan_dir.is_dir():
                    continue
                pm = plan_dir / "plan_metrics.csv"
                if not pm.exists():
                    continue
                with open(pm, newline="") as f:
                    for row in csv.DictReader(f):
                        plan_results.append(row)
                        break
            if not plan_results:
                continue

            # Load per_plan_ratings from selector_decision.json for this iteration
            ratings: Dict[str, str] = {}
            sel_dec = iter_k / "selector_decision.json"
            if sel_dec.exists():
                try:
                    sd = json.loads(sel_dec.read_text())
                    for r in sd.get("per_plan_ratings") or []:
                        ratings[r.get("plan", "")] = r.get("rating", "")
                except (json.JSONDecodeError, OSError):
                    pass

            seed_str = f"{seed_wns:.1f}" if seed_wns is not None else "?"
            detail_rows.append(f"  Iter{k} plan (seed WNS={seed_str} ps):")
            for row in plan_results:
                pname = row.get("plan", "?")
                status = row.get("status", "?")
                wns = row.get("wns", "") or "—"
                tns = row.get("tns", "") or "—"
                area = row.get("area_um2", "") or "—"
                moves = move_map.get(pname, "?")
                marker = ""
                rating = f"[{ratings[pname]}]" if pname in ratings else ""
                if status == "success":
                    try:
                        delta = f"{float(wns) - seed_wns:+.1f}" if seed_wns is not None else "?"
                    except (TypeError, ValueError):
                        delta = "?"
                    detail_rows.append(
                        f"    {pname}{rating}: {moves} → WNS={wns} (Δ={delta} vs seed) area={area}{marker}"
                    )
                else:
                    err = (row.get("first_error", "") or "")[:60]
                    detail_rows.append(
                        f"    {pname}{rating}: {moves} → {status} ({err})"
                    )
        detail_str = "\n".join(detail_rows) if detail_rows else ""

        # Compare current vs previous iteration worst paths
        prev_source = backtrack_from if backtrack_from else (iterN - 1)
        prev_worst_file = iter_dir(agent, design, prev_source) / "best" / "best_worst_paths.txt"
        comparison = ""
        if prev_worst_file.exists():
            prev_sections = _parse_path_sections(_read(prev_worst_file))
            cur_eps = {_get_endpoint(s).split("(")[0].strip() for s in sections}
            prev_eps = {_get_endpoint(s).split("(")[0].strip() for s in prev_sections}
            shared = cur_eps & prev_eps
            new_eps = cur_eps - prev_eps
            gone_eps = prev_eps - cur_eps
            comparison = (
                f"Path comparison vs Iteration {prev_source}:\n"
                f"  Shared endpoints (still violating): {len(shared)}/{len(cur_eps)}\n"
                f"  New endpoints (became critical): {len(new_eps)}"
                + (f" — {', '.join(list(new_eps)[:5])}" if new_eps else "")
                + f"\n  Resolved endpoints (no longer worst): {len(gone_eps)}"
                + (f" — {', '.join(list(gone_eps)[:5])}" if gone_eps else "")
            )

        prompt = f"""Design {design} — Iteration {iterN} Reporter Data

Clock domains (multi-clock designs: WNS/severity are relative to the path's OWN clock period):
{clock_summary}

Target: WNS ≥ 0 on every clock domain

{pdk_constraints}

Reference metrics:
  Base (post-CTS, no repair):    WNS={b_wns:.1f} TNS={b_tns:.1f} Area={b_area:.0f}um²({b_util:.0f}%) Viol={b_viol}
  Default (ORFS repair_timing):  WNS={d_wns:.1f} TNS={d_tns:.1f} Area={d_area:.0f}um²({d_util:.0f}%) Viol={d_viol}
  Default vs Base: ΔWNS={d_wns - b_wns:+.1f} ΔTNS={d_tns - b_tns:+.1f} ΔArea={d_area - b_area:+.0f}um² ΔViol={d_viol - b_viol:+d}

Current state (Iteration {iterN} seed — best of Iteration {prev_source}):
  WNS={s_wns:.3f} TNS={s_tns:.3f} Area={s_area:.0f}um²({s_util:.0f}%) Power={s_power:.4e}W Viol={s_viol}
  Progress: Base→Current ΔWNS={s_wns - b_wns:+.1f}ps over {iterN-1} iteration(s)
  Gap to default: {s_wns - d_wns:+.1f}ps | Gap to closure: {s_wns:.1f}ps remaining

{gr_summary if gr_summary.strip() else ""}

Iteration history (WNS trajectory across all prior iterations):
{history_str}

Recent plan attempts (last 3 iterations — see what was tried and the outcome):
{detail_str if detail_str else "  (none)"}

{comparison}

Current worst paths ({path_count} sections from seed, top {path_limit} endpoints):
{path_summary}

Endpoint violation registry (current seed):
{endpoint_registry}

Netlist neighborhood (current seed, top {neighbor_paths} endpoints):
{neighbor_netlist}

Cell catalog (cells on current worst paths):
{cell_catalog}

Placement report (current seed):
{placement_report if placement_report.strip() else "(not available)"}"""

        # Prior iteration: plan ratings, prediction calibration, selector strategy note
        prev_selector_path = iter_dir(agent, design, iterN - 1) / "selector_decision.json"
        if prev_selector_path.exists():
            try:
                prev_sel = json.loads(prev_selector_path.read_text())

                # --- 1. Per-plan ratings with sequences (evidence layer) ---
                ratings = prev_sel.get("per_plan_ratings") or []
                # Build sequence lookup from prior planner_decision.json
                seq_lookup: dict = {}
                prev_plan_path = iter_dir(agent, design, iterN - 1) / "planner_decision.json"
                if prev_plan_path.exists():
                    try:
                        prev_pd = json.loads(prev_plan_path.read_text())
                        for i, p in enumerate(prev_pd.get("plans") or [], start=1):
                            pid = f"plan{i}"
                            pt = p.get("plan_type", "sequence")
                            if pt == "staged":
                                seq = "staged:" + "→".join(
                                    s.get("move", "") for s in (p.get("stages") or [])
                                )
                            elif pt == "eco":
                                seq = "eco"
                            else:
                                seq = ",".join(p.get("sequence") or [])
                            seq_lookup[pid] = seq
                    except (json.JSONDecodeError, OSError):
                        pass

                if ratings:
                    rating_lines = ["\n\nPRIOR ITERATION PLAN RESULTS:"]
                    rating_lines.append(
                        "  (sequence used | Selector rating | outcome — read this before "
                        "deciding what to try next)"
                    )
                    for r in ratings:
                        pid    = r.get("plan", "?")
                        rating = r.get("rating", "?")
                        note   = r.get("note", "")
                        seq    = seq_lookup.get(pid, "?")
                        rating_lines.append(f"  {pid} | {seq} | [{rating}] {note}")
                    prompt += "\n".join(rating_lines)

                # --- 2. Prediction calibration (accuracy layer) ---
                pred_analysis = prev_sel.get("prediction_analysis") or []
                if pred_analysis:
                    cal_lines = ["\n\nPREDICTION CALIBRATION (your last iteration):"]
                    for pa in pred_analysis:
                        plan_id    = pa.get("plan", "?")
                        acc        = pa.get("accuracy", "").replace("_", " ")
                        mid        = pa.get("predicted_midpoint_ps")
                        actual     = pa.get("actual_delta_wns")
                        impl       = pa.get("implication", "")
                        pred_str   = f"~+{mid:.0f}ps" if mid is not None else "?"
                        actual_str = f"{actual:+.1f}ps" if actual is not None else "?"
                        cal_lines.append(
                            f"  {plan_id}: predicted {pred_str} → actual {actual_str} ({acc})"
                            + (f" — {impl}" if impl else "")
                        )
                    cal_lines.append(
                        "Use this to calibrate your expected_outcome estimates this iteration."
                    )
                    prompt += "\n".join(cal_lines)

                # --- 3. Selector strategy note (conclusion/guidance layer) ---
                sn    = prev_sel.get("strategy_note", {})
                stuck = sn.get("stuck_paths", [])
                diag  = sn.get("plateau_diagnosis")
                hints = sn.get("guidance", [])
                avoid = sn.get("avoid_strategies", [])
                if hints or stuck or diag or avoid:
                    note_parts = ["\n\nSELECTOR STRATEGY NOTE:"]
                    if diag:
                        note_parts.append(f"Plateau diagnosis: {diag}")
                    if stuck:
                        note_parts.append(f"Stuck paths: {', '.join(stuck)}")
                    for h in hints:
                        note_parts.append(f"Hint: {h}")
                    for a in avoid:
                        note_parts.append(f"AVOID: {a}")
                    prompt += "\n".join(note_parts)

            except (json.JSONDecodeError, OSError):
                pass

    out_path = idir / "reporter_baseline_prompt.txt"
    out_path.write_text(prompt + "\n", encoding="utf-8")
    log(f"Reporter prompt written: {out_path} (~{len(prompt)//4:,} tokens)")
    log(f"  Base WNS={b_wns:.2f}  Default WNS={d_wns:.2f}  period={period:.2f} ps")
    if iterN >= 2:
        log(f"  Seed WNS={s_wns:.1f}  (from {'backtrack iter ' + str(backtrack_from) if backtrack_from else 'iter ' + str(iterN-1)})")
    return True


# ===========================================================================
# SECTION 4 — Parallel plan workers (Docker / OpenROAD)
# ===========================================================================

def _classify_error(log_text: str, output_odb: pathlib.Path) -> Tuple[str, str]:
    if output_odb.exists():
        return "success", ""
    for line in log_text.splitlines():
        for pat in TCL_ERROR_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return "tcl_error", line.strip()
    for line in log_text.splitlines():
        for pat in OPENROAD_ERROR_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return "openroad_fail", line.strip()
    return "infra_error", "output.odb missing and no recognisable error pattern found"


def _parse_timing_file(p: pathlib.Path) -> Tuple[Optional[float], Optional[float]]:
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


def _parse_area_file(p: pathlib.Path) -> Tuple[Optional[float], Optional[float]]:
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


def _parse_viol_file(p: pathlib.Path) -> Optional[int]:
    if not p.exists():
        return None
    try:
        return int(p.read_text().splitlines()[0].strip())
    except (ValueError, IndexError):
        return None


def _collect_metrics(plan_dir: pathlib.Path, stage_tag: str) -> Dict:
    wns, tns   = _parse_timing_file(plan_dir / f"{stage_tag}_timing.txt")
    area, util = _parse_area_file(plan_dir / f"{stage_tag}_area.rpt")
    power      = _parse_power_file(plan_dir / f"{stage_tag}_power.rpt")
    viol       = _parse_viol_file(plan_dir / "violating_endpoint.txt")
    # Normalize raw timing values (native unit) to ps using the active PDK's factor
    tu = _PDK_CFG.time_unit_ps
    if wns is not None: wns *= tu
    if tns is not None: tns *= tu
    return {"wns": wns, "tns": tns, "area_um2": area, "util_percent": util,
            "power_mw": power, "violating_endpoints": viol}


def _append_log(log_file: pathlib.Path, text: str) -> None:
    with open(log_file, "a") as f:
        f.write(text)


def _write_plan_metrics(plan_dir: pathlib.Path, result: Dict) -> None:
    fields = ["plan", "status", "error_type", "first_error",
              "wns", "tns", "area_um2", "util_percent",
              "power_mw", "violating_endpoints", "log"]
    with open(plan_dir / "plan_metrics.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerow(result)


def _run_single_plan(plan_name: str, plan_dir: pathlib.Path,
                     seed_odb_path: pathlib.Path,
                     agent: str, design: str, iteration: int) -> Dict:
    log_file   = plan_dir / f"{plan_name}.log"
    tcl_file   = plan_dir / "run_plan.tcl"
    input_odb  = plan_dir / "input.odb"
    output_odb = plan_dir / "output.odb"

    result: Dict = {
        "plan": plan_name, "status": "infra_error", "error_type": "",
        "first_error": "", "wns": "", "tns": "", "area_um2": "",
        "util_percent": "", "power_mw": "", "violating_endpoints": "",
        "log": str(log_file),
    }

    if not tcl_file.exists():
        result["first_error"] = f"run_plan.tcl not found in {plan_dir}"
        _append_log(log_file, f"[INFRA] {result['first_error']}\n")
        return result

    try:
        shutil.copy2(seed_odb_path, input_odb)
    except OSError as e:
        result["first_error"] = f"Failed to copy seed ODB: {e}"
        _append_log(log_file, f"[INFRA] {result['first_error']}\n")
        return result

    sdc_file  = design_dir(agent, design) / "base" / "constraint.sdc"
    env_vars  = {
        "INPUT_DB":     to_docker(input_odb),
        "OUTPUT_DB":    to_docker(output_odb),
        "OUTPUT_DIR":   to_docker(plan_dir) + "/",
        "SDC_FILE":     to_docker(sdc_file),
        "LEF_DIR":      to_docker(LEF_DIR) + "/",
        "LIB_DIR":      to_docker(LIB_DIR) + "/",
        "SETRC_TCL":    to_docker(SETRC_TCL),
        "PDK_PREAMBLE": to_docker(WORKSPACE / _PDK_CFG.preamble_tcl),
        "STAGE_TAG":    plan_name,
        "FLOW_HOME":    DOCKER_FLOW_HOME,
        # GR signal layers + track adjustment — needed in every plan TCL
        # before pin_access/global_route (GRT state doesn't persist across
        # write_db/read_db).
        "MIN_ROUTING_LAYER":        _PDK_CFG.min_routing_layer,
        "MAX_ROUTING_LAYER":        _PDK_CFG.max_routing_layer,
        "ROUTING_LAYER_ADJUSTMENT": str(_PDK_CFG.routing_layer_adjustment),
    }

    cmd = ["docker", "run", "--rm", "--user", "root"]
    for k, v in env_vars.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["-v", f"{WORKSPACE}:/workspace", "-w", "/workspace"]
    cmd += [DOCKER_IMAGE, DOCKER_OPENROAD, "-no_init", "-exit", to_docker(tcl_file)]

    start = time.time()
    _append_log(log_file,
                f"=== {plan_name} — Iteration {iteration} ===\n"
                f"Command: {' '.join(cmd)}\n"
                f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{'=' * 60}\n\n")

    try:
        with open(log_file, "a") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)
        exit_code = proc.returncode
    except FileNotFoundError:
        result["first_error"] = "docker binary not found"
        _append_log(log_file, "\n[INFRA] docker not found in PATH\n")
        return result
    except Exception as e:
        result["first_error"] = f"subprocess error: {e}"
        _append_log(log_file, f"\n[INFRA] Subprocess error: {e}\n")
        return result

    elapsed = time.time() - start
    _append_log(log_file, f"\n{'=' * 60}\nExit code: {exit_code}  |  Elapsed: {elapsed:.1f}s\n")

    log_text = log_file.read_text(errors="replace")
    status, first_error = _classify_error(log_text, output_odb)
    result["status"]      = status
    result["first_error"] = first_error
    result["runtime_s"]   = elapsed

    if status == "success":
        metrics = _collect_metrics(plan_dir, plan_name)
        result.update({k: ("" if v is None else v) for k, v in metrics.items()})
    else:
        result["error_type"] = status

    _write_plan_metrics(plan_dir, result)
    return result


def _write_iteration_summary(idir: pathlib.Path, results: List[Dict]) -> None:
    fields = ["plan", "status", "error_type", "first_error",
              "wns", "tns", "area_um2", "util_percent",
              "power_mw", "violating_endpoints", "log"]
    with open(idir / "iteration_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)


def run_workers(agent: str, design: str, iteration: int,
                plan_count: int, plans: Optional[List[str]] = None) -> int:
    """Run the iteration's plan directory inside Docker. Returns 0 on success, 1 on failure."""
    idir       = iter_dir(agent, design, iteration)
    seed_path  = idir / "start" / "seed.odb"
    all_plans  = [f"plan{i}" for i in range(1, plan_count + 1)]
    targets    = plans if plans else all_plans

    if not seed_path.exists():
        log(f"[ERROR] Seed ODB not found: {seed_path}")
        return 2

    missing = [p for p in targets if not (idir / p).is_dir()]
    if missing:
        log(f"[WARN] Plan directories missing (likely rejected by Executor): {missing}")
        targets = [p for p in targets if p not in missing]
        if not targets:
            log("[ERROR] No valid plan directories to run.")
            return 2

    log(f"Workers: running {targets} (Iteration {iteration})")

    results: List[Dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(_run_single_plan, p, idir / p, seed_path,
                        agent, design, iteration): p
            for p in targets
        }
        for future in concurrent.futures.as_completed(futures):
            plan_name = futures[future]
            try:
                res = future.result()
            except Exception as e:
                res = {"plan": plan_name, "status": "infra_error",
                       "error_type": "infra_error", "first_error": str(e),
                       "wns": "", "tns": "", "area_um2": "", "util_percent": "",
                       "power_mw": "", "violating_endpoints": "",
                       "log": str(idir / plan_name / f"{plan_name}.log")}
            results.append(res)
            s = res["status"]
            if s == "success":
                log(f"  [{plan_name}] success | WNS={res['wns']} ps | "
                    f"TNS={res['tns']} ps | area={res['area_um2']} um²")
            else:
                log(f"  [{plan_name}] {s} | {str(res['first_error'])[:80]}")

    results.sort(key=lambda r: r["plan"])
    _write_iteration_summary(idir, results)

    # Record per-plan Docker wall-time
    for r in results:
        rt = r.get("runtime_s")
        if rt is not None:
            _record_runtime(agent, design, iteration, r["plan"], float(rt))

    log(f"Iteration summary → {idir}/iteration_summary.csv")
    log("Invoke Selector to evaluate results.")
    return 1 if any(r["status"] != "success" for r in results) else 0


# ===========================================================================
# SECTION 5 — Agent invocations (Claude CLI)
# ===========================================================================

MODEL_OPUS   = "claude-opus-4-7"       # Planner, Selector — reasoning-heavy
MODEL_SONNET = "claude-sonnet-4-6"     # Executor — TCL templating, mechanical


def _run_agent(label: str, prompt: str, claude_bin: str,
               model: Optional[str] = None,
               agent: Optional[str] = None,
               design: Optional[str] = None,
               iteration=None,
               role: Optional[str] = None,
               cwd: Optional[str] = None,
               resume_session: Optional[str] = None,
               session_file: Optional[pathlib.Path] = None,
               allowed_tools: Optional[str] = None,
               json_schema: Optional[str] = None,
               tools: Optional[str] = None) -> tuple:
    """Invoke Claude CLI in non-interactive JSON mode, capture usage.

    Returns (returncode: int, final_text: Optional[str]).
    `final_text` is the model's last message text — when --json-schema is used,
    this is the structured JSON output conforming to the schema.

    When `agent`/`design`/`role` are supplied, appends one row to tokens.csv
    with input/output/cache tokens, cost, and duration for the call.
    `cwd` defaults to WORKSPACE; pass a sandbox path to restrict file access.
    `resume_session`: if set, passes --resume <id> to continue a prior session.
    `session_file`: if set, writes the session_id from the response to this file.
    `json_schema`: if set, passes --json-schema to enforce structured output.
    `tools`: if set, passes --tools to control available tools (use "" to disable all)."""
    log(f"Invoking agent: {label}" + (f"  [model={model}]" if model else "")
        + (f"  [resume={resume_session[:8]}...]" if resume_session else ""))
    cmd = [
        claude_bin,
        "-p",                               # non-interactive print mode
        "--dangerously-skip-permissions",   # allow file reads/writes without prompts
        "--output-format", "json",          # emit usage/cost JSON
    ]
    if model:
        cmd += ["--model", model]
    if resume_session:
        cmd += ["--resume", resume_session]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if json_schema:
        cmd += ["--json-schema", json_schema]
    if tools is not None:
        cmd += ["--tools", tools]

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    t0 = time.monotonic()
    try:
        # Always pass prompt via stdin — avoids OS arg-length limits for long prompts
        # and prevents variadic flags (--allowedTools, --tools) from consuming the prompt.
        proc = subprocess.run(cmd, input=prompt, cwd=cwd or str(WORKSPACE),
                              capture_output=True, text=True)
    except FileNotFoundError as e:
        log(f"[FAIL] Agent {label}: {e}")
        return 127, None
    duration_s = time.monotonic() - t0

    result_obj = _parse_claude_json_output(proc.stdout)
    usage = (result_obj or {}).get("usage") or {}
    cost  = (result_obj or {}).get("total_cost_usd") or 0.0
    final_text = (result_obj or {}).get("result")
    if final_text:
        print(final_text, flush=True)
    elif proc.stdout:
        print(proc.stdout, flush=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, flush=True)

    # Persist session ID so the next call can resume this conversation
    session_id = (result_obj or {}).get("session_id")
    if session_file and session_id:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(session_id)

    if agent and design and role:
        iter_key = iteration if iteration is not None else "-"
        _record_tokens(agent, design, iter_key,
                       role, model or "", usage, float(cost),
                       duration_s, started_at)
        api_calls = _count_session_api_calls(session_id or "", cwd or str(WORKSPACE))
        if api_calls > 0:
            _record_apicalls(agent, design, iter_key, role, model or "", api_calls)

        # Copy session JSONL into the iteration directory for inspection
        if session_id and iteration is not None:
            try:
                projects_dir = pathlib.Path.home() / ".claude" / "projects"
                for project_dir in projects_dir.iterdir():
                    src = project_dir / f"{session_id}.jsonl"
                    if src.exists():
                        dest = iter_dir(agent, design, iteration) / f"{role}_convo.jsonl"
                        shutil.copy2(src, dest)
                        break
            except Exception:
                pass

    if proc.returncode != 0:
        log(f"[FAIL] Agent {label} exited {proc.returncode}")
    else:
        it = usage.get("input_tokens", 0)
        ot = usage.get("output_tokens", 0)
        log(f"[OK]   Agent {label} complete  "
            f"tokens in/out={it}/{ot}  cost=${cost:.4f}"
            + (f"  session={session_id[:8]}..." if session_id else ""))
    return proc.returncode, final_text



def _build_planner_sandbox(agent: str, design: str, iterN: int) -> pathlib.Path:
    """Create a minimal temp sandbox for the Planner. Empty except for the output path."""
    import tempfile
    sandbox = pathlib.Path(tempfile.mkdtemp(prefix=f"planner_{design}_iter{iterN}_"))
    iter_sandbox = (sandbox / "work_dir" / agent / PDK_NAME / design
                    / "llm_iterations" / f"iteration{iterN}")
    iter_sandbox.mkdir(parents=True)
    return sandbox


def _build_planner_context() -> str:
    """Build the combined reference text injected into the Planner prompt."""
    schema_json = (WORKSPACE / "scripts" / "schemas" / "planner_decision.schema.json").read_text()
    return "\n\n".join([
        (WORKSPACE / "agents" / "planner" / "AGENTS.md").read_text(),
        "---\n\n" + (WORKSPACE / "agents" / "openroad_reference.md").read_text(),
        "---\n\n" + (WORKSPACE / "agents" / f"{PDK_NAME}.md").read_text(),
        "---\n\n## Output Schema (planner_decision.schema.json)\n\n```json\n" + schema_json + "\n```",
    ])


def _pdk_hint() -> str:
    """Shared clause injected into every agent prompt stating which PDK is active."""
    base = f"PDK: {PDK_NAME}."
    if not _PDK_CFG.vt_tiers:
        base += (f" IMPORTANT: {PDK_NAME} is SINGLE-VT — do NOT propose vt_swap in any "
                 f"sequence/staged plan or include VT suffixes in ECO new_master values. "
                 f"OpenROAD silently no-ops vt_swap on single-VT PDKs.")
    return base


def invoke_planner(agent: str, design: str, iteration: int, claude_bin: str) -> bool:
    reporter_content = (iter_dir(agent, design, iteration) / "reporter_baseline_prompt.txt").read_text()
    agents_context = _build_planner_context()
    output_path = (f"work_dir/{agent}/{PDK_NAME}/{design}"
                   f"/llm_iterations/iteration{iteration}/planner_decision.json")
    prompt = (f"=== PLANNER REFERENCE (instructions + schema + PDK) ===\n{agents_context}\n"
              f"=== END REFERENCE ===\n\n"
              f"=== REPORTER OUTPUT ===\n{reporter_content}\n=== END REPORTER OUTPUT ===\n\n"
              f"Design: {design}, Agent: {agent}, Iteration: {iteration}. {_pdk_hint()}\n"
              f"All context is above. Do NOT use Bash. Do NOT read any files. "
              f"Do NOT verify your output after writing. "
              f"The output directory already exists. "
              f"Your only action: write planner_decision.json to: {output_path}")

    sandbox = _build_planner_sandbox(agent, design, iteration)
    try:
        rc, _ = _run_agent(f"Planner iter={iteration}", prompt, claude_bin,
                           model=MODEL_OPUS,
                           agent=agent, design=design,
                           iteration=iteration, role="planner",
                           cwd=str(sandbox),
                           allowed_tools="Write")
        sandbox_json = (sandbox / "work_dir" / agent / PDK_NAME / design
                        / "llm_iterations" / f"iteration{iteration}" / "planner_decision.json")
        real_json = iter_dir(agent, design, iteration) / "planner_decision.json"
        if sandbox_json.exists():
            shutil.copy2(sandbox_json, real_json)
            log(f"  Planner: copied planner_decision.json → {real_json}")
        elif rc == 0:
            log(f"  [WARN] Planner: planner_decision.json not found in sandbox")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    return rc == 0


def _build_executor_sandbox(agent: str, design: str, iterN: int) -> pathlib.Path:
    """Create a minimal temp directory to use as the Executor's cwd.

    Structure:
        <sandbox>/
            agents/executor/AGENTS.md                        → symlink (executor instructions)
            agents/openroad_reference.md                     → symlink (repair_timing knobs)
            agents/<pdk>.md                                  → symlink (cell naming/VT)
            scripts/schemas/planner_decision.schema.json     → symlink (input format)
            work_dir/<agent>/<design>/<pdk>/llm_iterations/iteration<N>/
                                                             → symlink to real iter dir

    The iteration symlink is a pass-through for writes: the Executor reads
    planner_decision.json and writes plan<i>/run_plan.tcl directly through it.
    No copy-out needed after the LLM call.
    """
    import tempfile
    sandbox = pathlib.Path(tempfile.mkdtemp(prefix=f"executor_{design}_iter{iterN}_"))

    # 1. Combined AGENTS.md — executor instructions + openroad_reference + PDK md + schema
    (sandbox / "agents" / "executor").mkdir(parents=True)
    schema_json = (WORKSPACE / "scripts" / "schemas" / "planner_decision.schema.json").read_text()
    combined = "\n\n".join([
        (WORKSPACE / "agents" / "executor" / "AGENTS.md").read_text(),
        "---\n\n" + (WORKSPACE / "agents" / "openroad_reference.md").read_text(),
        "---\n\n" + (WORKSPACE / "agents" / f"{PDK_NAME}.md").read_text(),
        "---\n\n## Input Schema (planner_decision.schema.json)\n\n```json\n" + schema_json + "\n```",
    ])
    (sandbox / "agents" / "executor" / "AGENTS.md").write_text(combined)

    # 2. planner_decision.json — only file the Executor needs to read from the iter dir
    iter_sandbox = (sandbox / "work_dir" / agent / PDK_NAME / design
                    / "llm_iterations" / f"iteration{iterN}")
    iter_sandbox.mkdir(parents=True)
    (iter_sandbox / "planner_decision.json").symlink_to(
        iter_dir(agent, design, iterN) / "planner_decision.json")

    # 3. plan<i>/ dirs — Executor writes run_plan.tcl here; pre-create via symlinks
    #    to the real plan dirs so writes land in the right place
    real_idir = iter_dir(agent, design, iterN)
    try:
        decision = json.loads((real_idir / "planner_decision.json").read_text())
        plan_count = len(decision.get("plans") or [])
    except Exception:
        plan_count = 1
    for pi in range(1, plan_count + 1):
        real_plan = real_idir / f"plan{pi}"
        real_plan.mkdir(parents=True, exist_ok=True)
        (iter_sandbox / f"plan{pi}").symlink_to(real_plan)

    return sandbox


def invoke_executor_retry(agent: str, design: str, iteration: int,
                          failures: List[Dict], claude_bin: str) -> bool:
    """LLM-based Executor retry — ask Claude to fix specific TCL errors in
    the failing plan scripts, then overwrite the offending run_plan.tcl files."""
    idir = iter_dir(agent, design, iteration)
    failure_summary = []
    for f in failures:
        plan = f.get("plan", "?")
        err  = f.get("first_error", "unknown error")
        failure_summary.append(f"  {plan}: {err}")
    failures_text = "\n".join(failure_summary)
    failed_plans  = [f["plan"] for f in failures]

    prompt = (
        f"Act as the Executor agent as defined in agents/executor/AGENTS.md. "
        f"Design: {design}, Agent: {agent}, Iteration: {iteration}. "
        f"{_pdk_hint()} "
        f"The following plan TCL scripts failed when run in OpenROAD Docker:\n"
        f"{failures_text}\n\n"
        f"For each failing plan:\n"
        f"  1. Read the current run_plan.tcl.\n"
        f"  2. If the error is a FIXABLE TCL syntax issue — write a corrected run_plan.tcl "
        f"to the SAME path. Fix ONLY the specific error, do not restructure the file.\n"
        f"     Common fixes: "
        f"(a) `-sequence` must use comma-separated moves not space-separated or braced; "
        f"(b) `resize_cell` → `replace_cell`; "
        f"(c) `unbuffer` → `remove_buffers`; "
        f"(d) instance names with `$` or `[` must be wrapped in `{{}}`.\n"
        f"  3. If the error is an UNFIXABLE OpenROAD error (tool bug, missing feature, "
        f"infrastructure failure) — do NOT rewrite the TCL. Instead append a single comment "
        f"line to the top of run_plan.tcl: `# UNFIXABLE: <reason>`.\n"
        f"Plan TCL paths: "
        f"work_dir/{agent}/{PDK_NAME}/{design}/llm_iterations/iteration{{iteration}}/<plan>/run_plan.tcl"
    )
    sandbox = _build_executor_sandbox(agent, design, iteration)
    try:
        rc, _ = _run_agent(f"Executor retry iter={iteration}", prompt, claude_bin,
                           model=MODEL_SONNET,
                           agent=agent, design=design,
                           iteration=iteration, role="executor_retry",
                           cwd=str(sandbox),
                           allowed_tools="Read,Write")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    if rc != 0:
        log(f"[WARN] Executor retry iter={iteration}: LLM call failed (rc={rc})")
        return False
    log(f"[OK]   Executor retry iter={iteration} — LLM applied fixes to {failed_plans}")
    return True



def _build_selector_sandbox(agent: str, design: str, iterN: int) -> pathlib.Path:
    """Create sandbox for the Selector with a symlink to the real design dir."""
    import tempfile
    sandbox = pathlib.Path(tempfile.mkdtemp(prefix=f"selector_{design}_iter{iterN}_"))
    (sandbox / ".claudeignore").write_text(
        "**/*_convo.jsonl\n"
        "**/reporter_baseline_prompt.txt\n"
        "**/plan*/*_worst_paths.txt\n"
        "**/*_netlist.v\n**/*.odb\n**/*.log\n**/run_logs/\n**/start/\n"
        "**/*_timing.txt\n**/*_area.rpt\n**/*_power.rpt\n**/output.sdc\n**/congestion.rpt\n"
    )
    work_agent = sandbox / "work_dir" / agent
    work_agent.mkdir(parents=True)
    (work_agent / PDK_NAME).symlink_to(design_dir(agent, design).parent)
    return sandbox


def _build_selector_context() -> str:
    """Combined reference text injected into the Selector prompt."""
    schema_json = (WORKSPACE / "scripts" / "schemas" / "selector_decision.schema.json").read_text()
    parts = [(WORKSPACE / "agents" / "selector" / "AGENTS.md").read_text()]
    params = WORKSPACE / "agents" / "selector" / "PARAMETERS.md"
    if params.exists():
        parts.append("---\n\n" + params.read_text())
    parts.append("---\n\n" + (WORKSPACE / "agents" / f"{PDK_NAME}.md").read_text())
    parts.append("---\n\n## Output Schema (selector_decision.schema.json)\n\n```json\n" + schema_json + "\n```")
    return "\n\n".join(parts)


def _build_selector_file_context(agent: str, design: str, iteration: int,
                                  best_plan: Optional[str]) -> str:
    """Pre-read filesystem files the Selector needs and format them for prompt injection.

    Reads:
    - Default metrics (budget reference)
    - Current iteration worst paths
    - Prior iteration worst paths (N >= 2)
    - Violating endpoints (for plateau analysis)
    """
    idir = iter_dir(agent, design, iteration)
    ddir = design_dir(agent, design)
    parts: list = []

    # Default metrics — budget reference
    default_metrics_path = ddir / "default" / "default_metrics.csv"
    if default_metrics_path.exists():
        parts.append(f"=== DEFAULT METRICS (budget reference) ===\n"
                     + default_metrics_path.read_text().strip())

    def _read_truncated(path: pathlib.Path, max_lines: int = 80) -> str:
        lines = path.read_text().strip().splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} lines truncated)"

    # Current iteration worst paths — try best/ first, fall back to plan dir
    cur_paths = idir / "best" / "best_worst_paths.txt"
    if not cur_paths.exists() and best_plan:
        cur_paths = idir / best_plan / f"{best_plan}_worst_paths.txt"
    if cur_paths.exists():
        parts.append(f"=== WORST PATHS (Iteration {iteration}) ===\n"
                     + _read_truncated(cur_paths))

    # Prior iteration worst paths
    if iteration >= 2:
        prior_paths = idir.parent / f"iteration{iteration - 1}" / "best" / "best_worst_paths.txt"
        if prior_paths.exists():
            parts.append(f"=== WORST PATHS (Iteration {iteration - 1}) ===\n"
                         + _read_truncated(prior_paths))

    # Violating endpoints
    viol = idir / "best" / "violating_endpoint.txt"
    if not viol.exists() and best_plan:
        viol = idir / best_plan / f"{best_plan}_violating_endpoint.txt"
    if viol.exists():
        parts.append(f"=== VIOLATING ENDPOINTS (Iteration {iteration}) ===\n"
                     + _read_truncated(viol, max_lines=50))

    # Prior backtrack targets — Selector cannot read past selector_decision.json files
    used_backtracks = []
    llm_root = idir.parent
    for k in range(1, iteration):
        sel_path = llm_root / f"iteration{k}" / "selector_decision.json"
        if sel_path.exists():
            try:
                sel = json.loads(sel_path.read_text())
                bt = sel.get("backtrack_to_iteration")
                if isinstance(bt, int):
                    used_backtracks.append(bt)
            except (OSError, json.JSONDecodeError):
                pass
    if used_backtracks:
        parts.append(f"=== PRIOR BACKTRACK TARGETS (already used — do not reuse) ===\n"
                     + ", ".join(f"Iteration {b}" for b in used_backtracks))

    return "\n\n".join(parts)


def invoke_selector(agent: str, design: str, iteration: int, claude_bin: str) -> bool:
    # Fresh session every iteration — no --resume (baseline behaviour).
    # See 2A-cont-context for the continuous-session variant.
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS_PY))
    import selector_prep as _sp

    ddir = design_dir(agent, design)

    # Pre-compute all mechanical parts before invoking the LLM
    try:
        ctx = _sp.build_context(
            iter_dir=iter_dir(agent, design, iteration),
            design_dir=ddir,
            iterN=iteration,
            pdk_name=PDK_NAME,
        )
    except Exception as e:
        log(f"[WARN] selector_prep.build_context failed: {e} — falling back to basic prompt")
        ctx = None

    # Build enriched prompt — AGENTS.md + schema injected, file context pre-read
    selector_context = _build_selector_context()
    file_context = _build_selector_file_context(
        agent, design, iteration, ctx.best_plan if ctx else None)
    output_path = (f"work_dir/{agent}/{PDK_NAME}/{design}"
                   f"/llm_iterations/iteration{iteration}/selector_decision.json")
    base_prompt = (
        f"=== SELECTOR REFERENCE (instructions + schema + PDK) ===\n{selector_context}\n"
        f"=== END REFERENCE ===\n\n"
        f"Design: {design}, Agent: {agent}, Iteration: {iteration}. {_pdk_hint()}\n"
        f"Do NOT read agents/selector/AGENTS.md or any schema file — they are provided above.\n"
        f"Do NOT include hints_to_planner or prediction_analysis in strategy_note — write guidance only.\n"
    )
    if ctx is not None and ctx.prompt_text:
        base_prompt += (
            f"\nPre-computed data — use directly, do NOT re-read from files:\n\n"
            f"{ctx.prompt_text}\n\n"
        )
    if file_context:
        base_prompt += (
            f"\nFilesystem data (worst paths, metrics) — pre-read for you:\n\n"
            f"{file_context}\n\n"
        )
    base_prompt += (
        f"Write your decision to: {output_path}\n"
        f"The output directory already exists. Do NOT use Bash to create directories."
    )

    sandbox = _build_selector_sandbox(agent, design, iteration)
    try:
        rc, _ = _run_agent(f"Selector iter={iteration}",
                           base_prompt,
                           claude_bin,
                           model=MODEL_OPUS,
                           agent=agent, design=design,
                           iteration=iteration, role="selector",
                           cwd=str(sandbox))
        # Merge pre-computed parts into the LLM-written selector_decision.json
        if rc == 0 and ctx is not None:
            ok = _sp.merge_selector_output(
                iter_dir=iter_dir(agent, design, iteration),
                ctx=ctx,
            )
            if not ok:
                log("[WARN] selector_prep.merge_selector_output failed — "
                    "selector_decision.json may be incomplete")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    return rc == 0


# ===========================================================================
# SECTION 6 — JSON helpers
# ===========================================================================

def _read_json(path: pathlib.Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        log(f"[ERROR] Failed to parse {path}: {e}")
        return None


def get_planner_decision(agent: str, design: str, iteration: int) -> Optional[dict]:
    path = iter_dir(agent, design, iteration) / "planner_decision.json"
    d    = _read_json(path)
    if d is None:
        log(f"[ERROR] planner_decision.json missing or invalid at {path}")
    return d


def get_selector_decision(agent: str, design: str, iteration: int) -> Optional[dict]:
    path = iter_dir(agent, design, iteration) / "selector_decision.json"
    d    = _read_json(path)
    if d is None:
        log(f"[ERROR] selector_decision.json missing or invalid at {path}")
    return d


# ===========================================================================
# SECTION 7 — Promote the iteration's chosen plan artifacts
# ===========================================================================

def promote_best(agent: str, design: str, iteration: int, decision: dict) -> bool:
    chosen = decision.get("chosen_plan")
    if not chosen:
        log("[WARN] No chosen_plan in selector decision — skipping promotion.")
        return False

    idir     = iter_dir(agent, design, iteration)
    plan_dir = idir / chosen
    best_dir = idir / "best"
    best_dir.mkdir(parents=True, exist_ok=True)

    copies = [
        (plan_dir / "output.odb",                        best_dir / "output.odb"),
        (plan_dir / f"{chosen}_timing.txt",              best_dir / "best_timing.txt"),
        (plan_dir / f"{chosen}_worst_paths.txt",         best_dir / "best_worst_paths.txt"),
        (plan_dir / f"{chosen}_area.rpt",                best_dir / "best_area.rpt"),
        (plan_dir / f"{chosen}_power.rpt",               best_dir / "best_power.rpt"),
        (plan_dir / f"{chosen}_metrics.csv",             best_dir / "best_metrics.csv"),
        (plan_dir / f"{chosen}_placement_report.txt",    best_dir / "best_placement_report.txt"),
        (plan_dir / f"{chosen}_netlist.v",               best_dir / "best_netlist.v"),
        (plan_dir / "violating_endpoint.txt",            best_dir / "violating_endpoint.txt"),
    ]
    missing = []
    for src, dst in copies:
        if src.exists():
            shutil.copy2(src, dst)
        else:
            missing.append(src.name)

    metrics = decision.get("chosen_metrics") or {}
    (best_dir / "best_plan.txt").write_text(
        f"plan={chosen}\n"
        f"wns={metrics.get('wns', '')}\n"
        f"tns={metrics.get('tns', '')}\n"
    )

    if missing:
        log(f"[WARN] Artifacts not found during promotion: {missing}")
    log(f"Promoted {chosen} → Iteration{iteration}/best/  (WNS={metrics.get('wns', '?')} ps)")
    return True


# ===========================================================================
# SECTION 8 — Best solutions tracker
# ===========================================================================

def update_best_solutions(agent: str, design: str, iteration: int, decision: dict) -> None:
    """Keep Best_solutions/rankings.json up to date with the top 4 plans by WNS.

    Compares ALL successful plans from the current iteration against the existing
    ranked list — not just the selector's chosen plan. Called after every iteration.
    """
    idir     = iter_dir(agent, design, iteration)
    chosen   = decision.get("chosen_plan", "")

    # --- Collect every successful plan from this iteration ---
    new_candidates: List[Dict] = []
    summary_csv = idir / "iteration_summary.csv"
    if summary_csv.exists():
        try:
            with open(summary_csv, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("status") != "success":
                        continue
                    try:
                        wns_val = float(row["wns"])
                        tns_val = float(row.get("tns") or 0)
                    except (ValueError, TypeError, KeyError):
                        continue
                    plan    = row["plan"]
                    plan_dir = idir / plan
                    odb_path = plan_dir / "output.odb"
                    sdc_path = plan_dir / "output.sdc"
                    new_candidates.append({
                        "iteration":   iteration,
                        "plan":        plan,
                        "wns_ps":      round(wns_val, 3),
                        "tns_ps":      round(tns_val, 3),
                        "odb":         str(odb_path),
                        "sdc":         str(sdc_path) if sdc_path.exists() else "",
                        "odb_exists":  odb_path.exists(),
                        "chosen":      plan == chosen,
                    })
        except OSError:
            pass

    if not new_candidates:
        log(f"Best_solutions: no successful plans in iteration {iteration} — skipping update.")
        return

    best_dir = design_dir(agent, design) / "llm_iterations" / "Best_solutions"
    best_dir.mkdir(parents=True, exist_ok=True)
    rankings_path = best_dir / "rankings.json"

    # Load existing rankings (drop all entries from this iteration to handle re-runs)
    existing: List[Dict] = []
    if rankings_path.exists():
        try:
            existing = json.loads(rankings_path.read_text()).get("rankings", [])
            existing = [r for r in existing if r.get("iteration") != iteration]
        except (json.JSONDecodeError, OSError):
            existing = []

    # Merge all candidates from this iteration with the surviving ranked entries,
    # sort by WNS descending (least negative = best), keep top 4
    pool = existing + new_candidates
    pool.sort(key=lambda r: r["wns_ps"], reverse=True)
    top4 = pool[:4]

    for i, r in enumerate(top4, 1):
        r["rank"] = i

    out = {
        "design":                 design,
        "agent":                  agent,
        "pdk":                    PDK_NAME,
        "last_updated_iteration": iteration,
        "last_updated":           time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": (
            "rank 1 = best WNS (closest to 0 ps). "
            "All successful iterations compete — not just selector-promoted ones. "
            "ODB paths reference original plan directories; files are not copied here."
        ),
        "rankings": top4,
    }

    rankings_path.write_text(json.dumps(out, indent=2) + "\n")

    rank1 = top4[0]
    new_in_top4 = [r for r in top4 if r.get("iteration") == iteration]
    log(f"Best_solutions updated (iter {iteration}: {len(new_candidates)} successful plans, "
        f"{len(new_in_top4)} entered top4) — "
        f"rank1: iter={rank1['iteration']} {rank1['plan']} WNS={rank1['wns_ps']} ps  "
        f"top4 WNS: {[r['wns_ps'] for r in top4]}")


# ===========================================================================
# SECTION 9 — Backend flow (route+finish → CSV generation)
# ===========================================================================

def _orfs_results(design: str) -> pathlib.Path:
    return ORFS_FLOW / "results" / PDK_NAME / design / "base"


def _orfs_logs(design: str) -> pathlib.Path:
    return ORFS_FLOW / "logs" / PDK_NAME / design / "base"


def _place_cts_for_routing(design: str,
                            odb_src: pathlib.Path,
                            sdc_src: pathlib.Path) -> None:
    """Copy ODB+SDC into ORFS results as 4_1_cts.* and 4_cts.* so that
    `make route finish` picks them up as the CTS handoff for a full GRT run."""
    dst     = to_docker(_orfs_results(design))
    src_odb = to_docker(odb_src)
    src_sdc = to_docker(sdc_src)
    cmd_str = (
        f"cp {src_odb} {dst}/4_1_cts.odb && "
        f"cp {src_sdc} {dst}/4_1_cts.sdc && "
        f"cp {src_odb} {dst}/4_cts.odb && "
        f"cp {src_sdc} {dst}/4_cts.sdc"
    )
    cmd = ["docker", "run", "--rm", "--user", "root",
           "-v", f"{WORKSPACE}:/workspace", DOCKER_IMAGE,
           "bash", "-c", cmd_str]
    rc = subprocess.run(cmd, capture_output=True).returncode
    if rc != 0:
        raise RuntimeError(f"Failed to place 4_1_cts files for {design} (exit {rc})")
    log(f"  [{design}] 4_1_cts.odb/sdc placed ({odb_src.name})")


def _run_route_finish(design: str, label: str, log_path: pathlib.Path) -> int:
    """Run full ORFS route+finish: clean_route then make route finish.
    Expects 4_1_cts.odb/sdc already placed in ORFS results by _place_cts_for_routing."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    orfs_flow_docker = f"/workspace/{_ORFS_CFG.root_dir_name}/flow"
    res = f"{orfs_flow_docker}/results/{PDK_NAME}/{design}/base"
    lgs = f"{orfs_flow_docker}/logs/{PDK_NAME}/{design}/base"
    rpt = f"{orfs_flow_docker}/reports/{PDK_NAME}/{design}/base"
    mk  = (f"DESIGN_CONFIG=./designs/{PDK_NAME}/{design}/config.mk "
           f"OPENROAD_EXE={DOCKER_OPENROAD} YOSYS_EXE={DOCKER_YOSYS} "
           f"SKIP_INCREMENTAL_REPAIR=1")
    cmd_str = (
        f"make clean_route {mk} && "
        f"rm -f {res}/6_*.odb {res}/6_*.v {res}/6_*.sdc "
        f"       {res}/6_*.def {res}/6_*.gds {res}/6_*.spef "
        f"       {lgs}/6_*.log {lgs}/6_*.json {rpt}/6_*.rpt && "
        f"make route finish {mk}"
    )
    cmd = ["docker", "run", "--rm", "--user", "root",
           "-e", f"FLOW_HOME={DOCKER_FLOW_HOME}",
           "-v", f"{WORKSPACE}:/workspace",
           "-w", f"{orfs_flow_docker}",
           DOCKER_IMAGE, "bash", "-c", cmd_str]
    log(f"  [{design}] Starting {label} route+finish → {log_path.name}")
    with open(log_path, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    status = "done" if rc == 0 else f"WARNING exit {rc}"
    log(f"  [{design}] {label} route+finish {status}")
    return rc




def _parse_route_metrics(design: str, stage: str) -> Optional[Dict]:
    """Parse GRT or DR metrics from ORFS JSON. Returns None if unavailable."""
    log_dir = _orfs_logs(design)
    if stage == "grt":
        jpath, prefix, lpath = (log_dir / "5_1_grt.json",
                                 "globalroute__", log_dir / "5_1_grt.log")
    else:
        jpath, prefix, lpath = (log_dir / "6_report.json",
                                 "finish__",       log_dir / "6_report.log")

    if not jpath.exists():
        log(f"  WARNING: {jpath.name} not found — metrics unavailable")
        return None
    try:
        d = json.loads(jpath.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f"  WARNING: failed to parse {jpath.name}: {e}")
        return None

    wns_key = f"{prefix}timing__setup__ws"
    if wns_key not in d:
        log(f"  WARNING: timing key absent in {jpath.name} — metrics unavailable")
        return None

    tu      = _PDK_CFG.time_unit_ps
    wns_ps  = round(d[wns_key] * tu, 3)
    tns_ps  = round(d.get(f"{prefix}timing__setup__tns", 0.0) * tu, 3)
    area    = d.get(f"{prefix}design__instance__area__stdcell", 0.0)
    util    = d.get(f"{prefix}design__instance__utilization__stdcell", 0.0)
    power_W = d.get(f"{prefix}power__total", 0.0)
    # Override area/util from log (handles macros whose area isn't in stdcell key)
    if lpath.exists():
        m = re.findall(r"Design area\s+([\d.]+)\s+um\^2\s+(\d+)%",
                       lpath.read_text(errors="ignore"))
        if m:
            area = float(m[-1][0])
            util = float(m[-1][1]) / 100.0

    return {"wns_ps": wns_ps, "tns_ps": tns_ps,
            "area_um2": round(area, 2), "util_pct": round(util * 100, 2),
            "power_W": power_W}


def _precompute_orfs_stage_metrics(design: str) -> Dict[str, Optional[Dict]]:
    """Read fp/gp/dp/cts metrics from ORFS JSON logs (produced by make cts-a)."""
    log_dir = _orfs_logs(design)
    tu      = _PDK_CFG.time_unit_ps
    stages  = {
        "fp_floorplan":    ("2_1_floorplan.json", "floorplan__",    "2_1_floorplan.log"),
        "gp_global_place": ("3_3_place_gp.json",  "globalplace__",  "3_3_place_gp.log"),
        "dp_detail_place": ("3_5_place_dp.json",  "detailedplace__","3_5_place_dp.log"),
        "cts_post_cts":    ("4_1_cts.json",       "cts__",          "4_1_cts.log"),
    }
    result: Dict[str, Optional[Dict]] = {}
    for stage, (jname, prefix, lname) in stages.items():
        jpath = log_dir / jname
        lpath = log_dir / lname
        if not jpath.exists():
            result[stage] = None
            continue
        try:
            d = json.loads(jpath.read_text())
        except (json.JSONDecodeError, OSError):
            result[stage] = None
            continue
        area    = d.get(f"{prefix}design__instance__area__stdcell", 0.0)
        util    = d.get(f"{prefix}design__instance__utilization__stdcell", 0.0)
        power_W = d.get(f"{prefix}power__total", 0.0)
        if lpath.exists():
            m = re.findall(r"Design area\s+([\d.]+)\s+um\^2\s+(\d+)%",
                           lpath.read_text(errors="ignore"))
            if m:
                area = float(m[-1][0])
                util = float(m[-1][1]) / 100.0
        result[stage] = {
            "wns_ps":   round(d.get(f"{prefix}timing__setup__ws",  0.0) * tu, 3),
            "tns_ps":   round(d.get(f"{prefix}timing__setup__tns", 0.0) * tu, 3),
            "area_um2": round(area, 2),
            "util_pct": round(util * 100, 2),
            "power_W":  power_W,
        }
    return result


def _read_default_repair_metrics(agent: str, design: str) -> Optional[Dict]:
    """Read default repair_timing metrics from work_dir default artifact files."""
    ddir = design_dir(agent, design) / "default"
    tu   = _PDK_CFG.time_unit_ps
    try:
        wns_raw, tns_raw = _parse_timing(_read(ddir / "default_timing.txt"))
        area, util_pct   = _parse_area_util(_read(ddir / "default_area.rpt"))
        power            = _parse_power(_read(ddir / "default_power.rpt"))
        return {"wns_ps":   round(wns_raw * tu, 3),
                "tns_ps":   round(tns_raw * tu, 3),
                "area_um2": round(area, 2),
                "util_pct": round(util_pct, 2),   # _parse_area_util returns %
                "power_W":  power}
    except SystemExit:
        return None


def _read_plan_repair_metrics(plan_dir: pathlib.Path, plan_name: str) -> Optional[Dict]:
    """Read post-repair metrics for a plan from its timing/area/power artifact files."""
    tu = _PDK_CFG.time_unit_ps
    wns_raw, tns_raw = _parse_timing_file(plan_dir / f"{plan_name}_timing.txt")
    area, util_pct   = _parse_area_file(plan_dir / f"{plan_name}_area.rpt")
    power            = _parse_power_file(plan_dir / f"{plan_name}_power.rpt")
    if wns_raw is None:
        return None
    return {"wns_ps":   round(wns_raw * tu, 3),
            "tns_ps":   round((tns_raw or 0.0) * tu, 3),
            "area_um2": round(area or 0.0, 2),
            "util_pct": round(util_pct or 0.0, 2),   # _parse_area_file returns %
            "power_W":  power or 0.0}


def _fmt_csv_row(stage: str, m: Optional[Dict]) -> List:
    """Return a CSV row list, using N/A for any unavailable metric dict."""
    if m is None:
        return [stage, "N/A", "N/A", "N/A", "N/A", "N/A"]
    na = "N/A"
    def _f(key, fmt):
        v = m.get(key)
        return (fmt % v) if v is not None else na
    return [stage,
            _f("wns_ps",   "%.3f"),
            _f("tns_ps",   "%.3f"),
            _f("area_um2", "%.2f"),
            _f("util_pct", "%.2f"),
            _f("power_W",  "%.6e")]


def _write_flow_csv(path: pathlib.Path, rows: List[List]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stage", "wns_ps", "tns_ps", "area_um2", "util_pct", "power_W"])
        w.writerows(rows)


def _build_experiment_summary_csv(agent: str, design: str) -> pathlib.Path:
    """Build experiment_summary.csv from all iteration selector + plan data."""
    out      = design_dir(agent, design) / "experiment_summary.csv"
    llm_root = design_dir(agent, design) / "llm_iterations"
    all_rows: List[Dict] = []

    # Pre-pass: cache each iteration's selector outcome so we can look up the
    # seed (prior-iteration or backtrack-target) for every row.
    iter_data: Dict[int, Dict] = {}
    for k in range(1, 200):
        kd = llm_root / f"iteration{k}"
        if not kd.exists():
            break
        sp = kd / "selector_decision.json"
        if not sp.exists():
            continue
        s = json.loads(sp.read_text())
        cm = s.get("chosen_metrics") or {}
        iter_data[k] = {
            "plan":         s.get("chosen_plan", ""),
            "wns":          cm.get("wns"),
            "tns":          cm.get("tns"),
            "backtrack_to": s.get("backtrack_to_iteration"),
        }

    for iter_n in range(1, 200):
        iter_d   = llm_root / f"iteration{iter_n}"
        if not iter_d.exists():
            break
        sel_path = iter_d / "selector_decision.json"
        pln_path = iter_d / "planner_decision.json"
        sum_path = iter_d / "iteration_summary.csv"
        if not sel_path.exists() or not sum_path.exists():
            continue

        sel       = json.loads(sel_path.read_text())
        pln       = json.loads(pln_path.read_text()) if pln_path.exists() else {}
        best_plan = sel.get("chosen_plan", "")

        # Build plan-name → {plan_type, sequence_or_moves} from planner JSON
        plan_info: Dict[str, Dict] = {}
        for i, p in enumerate((pln.get("plans") or []), 1):
            pt = p.get("plan_type", "sequence")
            if pt == "staged":
                moves = " | ".join(s.get("move", "") for s in p.get("stages", []))
            elif pt == "eco":
                moves = str(p.get("eco_changes", ""))
            else:
                moves = ",".join(p.get("sequence", []))
            plan_info[f"plan{i}"] = {"plan_type": pt, "sequence_or_moves": moves}

        conv         = sel.get("convergence") or {}
        chosen_m     = sel.get("chosen_metrics") or {}
        it_table     = {r["iter"]: r for r in (sel.get("iteration_table") or [])}
        seed_wns     = (it_table.get(iter_n) or {}).get("wns_in", None)

        # Resolve seed iteration/plan for this iteration.
        # Iter 1 → seeded from "base". Iter N≥2 → prev selector's backtrack_to
        # if set, otherwise iter N-1.
        if iter_n == 1:
            seed_iter_label = "base"
            seed_plan_label = "base"
            seed_tns        = None
        else:
            prev = iter_data.get(iter_n - 1, {})
            bt   = prev.get("backtrack_to")
            seed_src = bt if bt else (iter_n - 1)
            seed_iter_label = seed_src
            src_entry = iter_data.get(seed_src, {})
            seed_plan_label = src_entry.get("plan", "")
            seed_tns        = src_entry.get("tns")

        with open(sum_path, newline="") as f:
            for row in csv.DictReader(f):
                pname = row["plan"]
                wns   = row.get("wns", "")
                delta = ""
                if seed_wns is not None and wns:
                    try:
                        delta = f"{float(wns) - seed_wns:+.3f}"
                    except ValueError:
                        pass
                pi = plan_info.get(pname, {})
                all_rows.append({
                    "iteration":         iter_n,
                    "plan":              pname,
                    "is_best":           "YES" if pname == best_plan else "",
                    "plan_type":         pi.get("plan_type", ""),
                    "sequence_or_moves": pi.get("sequence_or_moves", ""),
                    "seed_iteration":    seed_iter_label,
                    "seed_plan":         seed_plan_label,
                    "before_wns":        seed_wns if seed_wns is not None else "",
                    "before_tns":        seed_tns if seed_tns is not None else "",
                    "status":            row.get("status", ""),
                    "after_wns":         wns,
                    "after_tns":         row.get("tns", ""),
                    "area_um2":          row.get("area_um2", ""),
                    "util_pct":          row.get("util_percent", ""),
                    "power_mw":          row.get("power_mw", ""),
                    "violations":        row.get("violating_endpoints", ""),
                    "delta_vs_seed":     delta,
                    "error":             row.get("first_error", ""),
                    "iter_best_wns":     chosen_m.get("wns", ""),
                    "iter_best_tns":     chosen_m.get("tns", ""),
                    "iter_delta_wns":    conv.get("delta_wns_this_iter", ""),
                    "iter_plateau":      conv.get("plateau_status", ""),
                    "iter_decision":     sel.get("decision", ""),
                    "iter_backtrack_to": sel.get("backtrack_to_iteration", ""),
                })

    fields = ["iteration", "plan", "is_best", "plan_type", "sequence_or_moves",
              "seed_iteration", "seed_plan", "status",
              "before_wns", "after_wns", "before_tns", "after_tns",
              "area_um2", "util_pct", "power_mw",
              "violations", "delta_vs_seed", "error",
              "iter_best_wns", "iter_best_tns", "iter_delta_wns",
              "iter_plateau", "iter_decision", "iter_backtrack_to"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)
    log(f"experiment_summary.csv → {out}  ({len(all_rows)} rows)")
    return out


def _read_baseline_dr_from_csv(agent: str, design: str) -> Optional[Dict]:
    """Read the dr_post_route row from {design}_baseline.csv. Returns None if unavailable."""
    path = design_dir(agent, design) / f"{design}_baseline.csv"
    if not path.exists():
        return None
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("stage") == "dr_post_route":
                    out = {}
                    for key, field in [("wns_ps", "wns_ps"), ("tns_ps", "tns_ps"),
                                       ("area_um2", "area_um2"), ("util_pct", "util_pct"),
                                       ("power_W", "power_W")]:
                        v = row.get(field, "N/A")
                        if v == "N/A" or v == "":
                            return None
                        out[key] = float(v)
                    return out
    except (OSError, ValueError):
        return None
    return None


def run_backend_baseline(agent: str, design: str) -> int:
    """Baseline route+finish using default.odb → writes {design}_baseline.csv."""
    banner(f"BACKEND baseline  design={design}  agent={agent}  pdk={PDK_NAME}")
    ddir = design_dir(agent, design)

    default_odb = ddir / "default" / "default.odb"
    default_sdc = ddir / "default" / "constraint.sdc"
    for p in (default_odb, default_sdc):
        if not p.exists():
            log(f"[ERROR] Backend baseline: required file missing: {p}")
            return 1

    log_dir = ddir / "run_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Copy default.odb as 4_1_cts.odb so ORFS runs full GRT + detail route.
    _place_cts_for_routing(design, default_odb, default_sdc)
    _run_route_finish(design, "baseline",
                      log_dir / "route_finish_baseline.log")
    grt = _parse_route_metrics(design, "grt")
    dr  = _parse_route_metrics(design, "dr")
    if grt and dr:
        log(f"Baseline: GRT WNS={grt['wns_ps']:.3f} ps  DR WNS={dr['wns_ps']:.3f} ps")

    orfs_stages = _precompute_orfs_stage_metrics(design)
    default_m   = _read_default_repair_metrics(agent, design)

    baseline_csv = ddir / f"{design}_baseline.csv"
    _write_flow_csv(baseline_csv, [
        _fmt_csv_row(s, orfs_stages.get(s))
        for s in ("fp_floorplan", "gp_global_place", "dp_detail_place", "cts_post_cts")
    ] + [
        _fmt_csv_row("default_repair",   default_m),
        _fmt_csv_row("grt_global_route", grt),
        _fmt_csv_row("dr_post_route",    dr),
    ])
    log(f"Written: {baseline_csv}")

    banner(f"BACKEND baseline complete  DR WNS={dr['wns_ps'] if dr else 'N/A'} ps")
    return 0


def run_backend_rank(agent: str, design: str, rank: int) -> int:
    """Rank-N agentic route+finish → writes {design}_agentic[_rN].csv + comparison CSV.

    Rank 1 → {design}_agentic.csv + final_comparison.csv
    Rank N → {design}_agentic_r{N}.csv + rank{N}_comparison.csv
    Also writes experiment_summary.csv.
    Requires {design}_baseline.csv from a prior backend_default run.
    """
    banner(f"BACKEND rank{rank}  design={design}  agent={agent}  pdk={PDK_NAME}")
    ddir = design_dir(agent, design)

    # experiment_summary.csv (from iteration data, no routing needed)
    _build_experiment_summary_csv(agent, design)

    # Locate rank-N entry in Best_solutions
    rankings_path = ddir / "llm_iterations" / "Best_solutions" / "rankings.json"
    if not rankings_path.exists():
        log(f"[ERROR] Backend rank{rank}: Best_solutions/rankings.json not found — "
            f"run --run-stage LLM-iterations first.")
        return 1
    try:
        rankings: List[Dict] = json.loads(rankings_path.read_text()).get("rankings", [])
    except (json.JSONDecodeError, OSError) as e:
        log(f"[ERROR] Backend rank{rank}: cannot read rankings.json: {e}")
        return 1

    entry = next((r for r in rankings if r.get("rank") == rank), None)
    if entry is None:
        available = [r.get("rank") for r in rankings]
        log(f"[ERROR] Backend rank{rank}: rank {rank} not found (available: {available})")
        return 1

    agentic_odb = pathlib.Path(entry["odb"])
    agentic_sdc = pathlib.Path(entry["sdc"]) if entry.get("sdc") else None
    agentic_label = f"agentic_iter{entry['iteration']}_{entry['plan']}"

    if not agentic_odb.exists():
        log(f"[ERROR] Backend rank{rank}: ODB missing: {agentic_odb}")
        return 1
    if not agentic_sdc or not agentic_sdc.exists():
        fallback_sdc = design_dir(agent, design) / "base" / "constraint.sdc"
        if fallback_sdc.exists():
            log(f"[WARN] Backend rank{rank}: plan output.sdc missing — "
                f"falling back to base constraint.sdc (plan did not modify timing constraints).")
            agentic_sdc = fallback_sdc
        else:
            log(f"[ERROR] Backend rank{rank}: SDC missing and base constraint.sdc not found.")
            return 1

    log(f"Agentic: iter={entry['iteration']} {entry['plan']} "
        f"WNS={entry['wns_ps']} ps  ({agentic_odb.name})")

    # Read baseline DR from prior baseline CSV (for comparison row)
    baseline_dr = _read_baseline_dr_from_csv(agent, design)
    if baseline_dr is None:
        log(f"[WARN] {design}_baseline.csv not found or DR row missing — "
            f"comparison will use N/A. Run --run-stage backend_default first.")

    log_dir = ddir / "run_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Copy best ODB as 4_1_cts.odb so ORFS runs full GRT + detail route.
    _place_cts_for_routing(design, agentic_odb, agentic_sdc)
    _run_route_finish(design, f"rank{rank}",
                      log_dir / f"route_finish_rank{rank}.log")
    grt = _parse_route_metrics(design, "grt")
    dr  = _parse_route_metrics(design, "dr")
    if grt and dr:
        log(f"Agentic : GRT WNS={grt['wns_ps']:.3f} ps  DR WNS={dr['wns_ps']:.3f} ps")

    orfs_stages = _precompute_orfs_stage_metrics(design)
    agentic_m   = _read_plan_repair_metrics(
        iter_dir(agent, design, entry["iteration"]) / entry["plan"],
        entry["plan"],
    )

    # Choose output filenames based on rank
    if rank == 1:
        agentic_csv_name = f"{design}_agentic.csv"
        comp_csv_name    = "final_comparison.csv"
    else:
        agentic_csv_name = f"{design}_agentic_r{rank}.csv"
        comp_csv_name    = f"rank{rank}_comparison.csv"

    # {design}_agentic[_rN].csv
    agentic_csv = ddir / agentic_csv_name
    _write_flow_csv(agentic_csv, [
        _fmt_csv_row(s, orfs_stages.get(s))
        for s in ("fp_floorplan", "gp_global_place", "dp_detail_place", "cts_post_cts")
    ] + [
        _fmt_csv_row(agentic_label,      agentic_m),
        _fmt_csv_row("grt_global_route", grt),
        _fmt_csv_row("dr_post_route",    dr),
    ])
    log(f"Written: {agentic_csv}")

    # Comparison CSV
    comp_csv = ddir / comp_csv_name
    comp_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(comp_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["flow", "wns_ps", "tns_ps", "area_um2", "util_pct", "power_W"])
        w.writerow(_fmt_csv_row("default_baseline", baseline_dr))
        w.writerow(_fmt_csv_row(agentic_label,      dr))
        if baseline_dr and dr:
            w.writerow([
                f"delta_{agentic_label}_minus_default",
                f"{dr['wns_ps']  - baseline_dr['wns_ps']:+.3f}",
                f"{dr['tns_ps']  - baseline_dr['tns_ps']:+.3f}",
                f"{dr['area_um2']- baseline_dr['area_um2']:+.2f}",
                f"{dr['util_pct']- baseline_dr['util_pct']:+.2f}",
                f"{dr['power_W'] - baseline_dr['power_W']:+.6e}",
            ])
        else:
            w.writerow([f"delta_{agentic_label}_minus_default",
                        "N/A", "N/A", "N/A", "N/A", "N/A"])
    log(f"Written: {comp_csv}")

    banner(f"BACKEND rank{rank} complete  "
           f"baseline DR={baseline_dr['wns_ps'] if baseline_dr else 'N/A'} ps  "
           f"agentic DR={dr['wns_ps'] if dr else 'N/A'} ps")
    return 0


# ===========================================================================
# SECTION 10 — LLM-iterations loop
# ===========================================================================

def run_llm_iterations(agent: str, design: str, claude: str,
                        max_iterations: int, start_iteration: int) -> int:
    """Full agentic loop: Reporter → Planner → Executor → Workers → Selector per iteration."""
    banner(f"LLM ITERATIONS  design={design}  agent={agent}  pdk={PDK_NAME}  "
           f"max_iter={max_iterations}  start={start_iteration}")

    wns = "?"
    backtrack_count = 0
    backtrack_override: Dict[int, int] = {}

    for iteration in range(start_iteration, max_iterations + 1):
        banner(f"ITERATION {iteration} / {max_iterations}")
        _iter_started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        _iter_t0 = time.monotonic()
        try:

            # 1. Reporter
            log("--- Step 1: Reporter ---")
            bt_from = backtrack_override.pop(iteration, None)
            with _time_stage(agent, design, iteration, "reporter"):
                if not run_reporter(agent, design, iteration, backtrack_from=bt_from):
                    log(f"[ERROR] Reporter failed at iteration {iteration}. Stopping.")
                    return 1

            # 2. Planner
            log("--- Step 2: Planner ---")
            with _time_stage(agent, design, iteration, "planner"):
                if not invoke_planner(agent, design, iteration, claude):
                    log(f"[ERROR] Planner failed at iteration {iteration}. Stopping.")
                    return 1

                decision = get_planner_decision(agent, design, iteration)
                if decision is None:
                    return 1

                if decision.get("decision") != "execute":
                    log(f"[ERROR] Unexpected planner decision '{decision.get('decision')}'. Stopping.")
                    return 1

            plan_count = decision.get("plan_count", len(decision.get("plans", [])))
            log(f"Planner committed the iteration's plan.")

            # 3. Executor — pure Python generates the iteration's run_plan.tcl.
            # LLM executor only kicks in at step 4b if the plan fails in Docker.
            log("--- Step 3: Executor (Python) ---")
            import executor as _executor
            _executor_ok, _executor_errs = _executor.build_plans(iter_dir(agent, design, iteration))
            if not _executor_ok:
                log(f"[ERROR] Executor (Python) failed at iteration {iteration} — no valid plans:")
                for e in _executor_errs:
                    log(f"  {e}")
                return 1
            if _executor_errs:
                for w in _executor_errs:
                    log(f"  [WARN] {w}")
            log(f"  Executor: TCL generated for the iteration's plan.")

            # 4. Workers
            log("--- Step 4: Workers ---")
            with _time_stage(agent, design, iteration, "workers_total"):
                rc = run_workers(agent, design, iteration, plan_count)
            if rc == 2:
                log(f"[ERROR] Infrastructure error running workers at iteration {iteration}. Stopping.")
                return 1

            # 4b. Retry TCL errors
            idir_path = iter_dir(agent, design, iteration)
            summary_csv = idir_path / "iteration_summary.csv"
            if summary_csv.exists():
                tcl_failures = []
                with open(summary_csv, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("status") == "tcl_error":
                            tcl_failures.append(row)
                if tcl_failures:
                    failed_names = [f["plan"] for f in tcl_failures]
                    log(f"--- Step 4b: Executor repair for TCL errors: {failed_names} ---")

                    # First pass: deterministic Python fixes (fast, free)
                    fixed_count, unfixable_det = _executor.fix_tcl_errors(
                        tcl_failures, iter_dir(agent, design, iteration))
                    if fixed_count:
                        log(f"  Python fixes applied to {fixed_count} plan(s).")

                    # Remaining failures → LLM repair
                    still_broken = [f for f in tcl_failures
                                    if f["plan"] in [u.split(":")[0].strip() for u in unfixable_det]]
                    if not still_broken:
                        still_broken = tcl_failures if fixed_count == 0 else []

                    if still_broken:
                        log(f"  LLM repair for: {[f['plan'] for f in still_broken]}")
                        with _time_stage(agent, design, iteration, "executor_retry"):
                            retry_ok = invoke_executor_retry(
                                agent, design, iteration, still_broken, claude)
                        if not retry_ok:
                            log("[WARN] LLM repair failed — marking remaining plans as unfixable.")
                            still_broken = []  # skip re-run for these

                    # Re-run the plan if it got fixed (deterministic or LLM)
                    rerun = [f["plan"] for f in tcl_failures
                             if f["plan"] not in [u.split(":")[0].strip() for u in unfixable_det]
                             or fixed_count > 0]
                    if rerun:
                        log(f"  Re-running repaired plans: {rerun}")
                        with _time_stage(agent, design, iteration, "workers_retry"):
                            run_workers(agent, design, iteration, plan_count, plans=rerun)
                    else:
                        log("  All failed plans classified as unfixable — proceeding with partial results.")

            # 5. Selector
            log("--- Step 5: Selector ---")
            with _time_stage(agent, design, iteration, "selector"):
                if not invoke_selector(agent, design, iteration, claude):
                    log(f"[ERROR] Selector failed at iteration {iteration}. Stopping.")
                    return 1

                sel = get_selector_decision(agent, design, iteration)
                if sel is None:
                    return 1

            sel_decision = sel.get("decision", "continue")
            conv = sel.get("convergence", {})
            wns  = (sel.get("chosen_metrics") or {}).get("wns", "?")

            if sel_decision == "stop":
                promote_best(agent, design, iteration, sel)
                update_best_solutions(agent, design, iteration, sel)
                reason = sel.get("stop_reason", "unknown")
                banner(f"LOOP STOPPED — reason: {reason}  final WNS={wns} ps")
                return 0

            elif sel_decision == "backtrack":
                bt_iter = sel.get("backtrack_to_iteration", 1)
                backtrack_count += 1
                promote_best(agent, design, iteration, sel)
                backtrack_seed = iter_dir(agent, design, bt_iter) / "best" / "output.odb"
                if not backtrack_seed.exists():
                    log(f"[ERROR] Backtrack target Iteration{bt_iter}/best/output.odb not found. Stopping.")
                    return 1
                backtrack_override[iteration + 1] = bt_iter
                log(f"BACKTRACK #{backtrack_count}: next iteration will seed from "
                    f"iteration{bt_iter}/best/output.odb")

            elif sel_decision == "accept_regression":
                promote_best(agent, design, iteration, sel)
                log(f"ACCEPT REGRESSION: promoting regressed plan (WNS={wns} ps)")

            elif sel_decision == "retry":
                if iteration == 1:
                    promote_best(agent, design, iteration, sel)
                    log("RETRY at iter 1: no prior best to carry forward — promoted chosen plan")
                else:
                    src_best = iter_dir(agent, design, iteration - 1) / "best"
                    dst_best = iter_dir(agent, design, iteration) / "best"
                    if src_best.exists():
                        dst_best.mkdir(parents=True, exist_ok=True)
                        for f in src_best.iterdir():
                            if f.is_file():
                                shutil.copy2(f, dst_best / f.name)
                        log(f"RETRY: iter {iteration}/best/ carries forward iter {iteration-1}/best/")
                    else:
                        log(f"[ERROR] retry but iter {iteration-1}/best/ missing. Stopping.")
                        return 1

            else:  # "continue"
                promote_best(agent, design, iteration, sel)

            update_best_solutions(agent, design, iteration, sel)
            log(f"Iteration {iteration} complete — WNS={wns} ps  "
                f"ΔWNS={conv.get('delta_wns_this_iter', '?')} ps  "
                f"plateau={conv.get('plateau_status', '?')}")

        finally:
            _record_runtime(agent, design, iteration,
                            "iteration_total",
                            time.monotonic() - _iter_t0,
                            _iter_started_at)
    banner(f"MAX ITERATIONS REACHED ({max_iterations})  final WNS={wns} ps")
    return 0


# ===========================================================================
# SECTION 11 — Clean targets
# ===========================================================================

def _rmtree_via_docker(paths: List[str]) -> None:
    """Use Docker root to delete root-owned paths. `paths` are host absolute paths."""
    docker_paths = [to_docker(pathlib.Path(p)) for p in paths]
    rm_cmd = " ".join(f"rm -rf {p}" for p in docker_paths)
    cmd = ["docker", "run", "--rm", "--user", "root",
           "-v", f"{WORKSPACE}:/workspace", DOCKER_IMAGE,
           "bash", "-c", rm_cmd]
    subprocess.run(cmd, check=False, capture_output=True)


def clean_base(agent: str, design: str) -> None:
    """Delete ORFS tree for design (base requires full rebuild) + work_dir base/."""
    banner(f"CLEAN base  design={design}  pdk={PDK_NAME}")
    ddir = design_dir(agent, design)
    _rmtree_via_docker([
        str(ORFS_FLOW / "results" / PDK_NAME / design),
        str(ORFS_FLOW / "logs"    / PDK_NAME / design),
        str(ORFS_FLOW / "reports" / PDK_NAME / design),
    ])
    if (ddir / "base").exists():
        shutil.rmtree(ddir / "base")
    log("base cleaned.")


def clean_default(agent: str, design: str) -> None:
    """Delete work_dir default/."""
    banner(f"CLEAN default  design={design}")
    ddir = design_dir(agent, design)
    if (ddir / "default").exists():
        shutil.rmtree(ddir / "default")
    log("default cleaned.")


def clean_agentic_flow(agent: str, design: str) -> None:
    """Delete work_dir llm_iterations/ and reset tokens.csv + iteration rows in runtimes.csv."""
    banner(f"CLEAN agentic_flow  design={design}")
    ddir = design_dir(agent, design)
    if (ddir / "llm_iterations").exists():
        _rmtree_via_docker([str(ddir / "llm_iterations")])

    # Reset tokens.csv — all rows are iteration-specific
    tpath = _tokens_csv(agent, design)
    if tpath.exists():
        tpath.write_text(",".join(_TOKEN_FIELDS) + "\n")
        log("tokens.csv reset.")

    # Reset apicall.csv
    apath = _apicall_csv(agent, design)
    if apath.exists():
        apath.write_text(",".join(_APICALL_FIELDS) + "\n")
        log("apicall.csv reset.")

    # Clear selector session so the next run starts a fresh conversation
    sfile = ddir / "selector_session_id.txt"
    if sfile.exists():
        sfile.unlink()
        log("selector_session_id.txt cleared.")

    # Reset runtimes.csv — keep only iteration="-" rows (base/default stage timings)
    rpath = _runtimes_csv(agent, design)
    if rpath.exists():
        with open(rpath, newline="") as f:
            rows = list(csv.DictReader(f))
        keep = [r for r in rows if r.get("iteration") == "-"]
        with open(rpath, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_RUNTIME_FIELDS)
            w.writeheader()
            w.writerows(keep)
        log(f"runtimes.csv reset (kept {len(keep)} base/default row(s)).")

    log("agentic_flow cleaned.")


def clean_all(agent: str, design: str) -> None:
    """Delete everything: ORFS tree, work_dir, backend run_logs, all CSVs."""
    banner(f"CLEAN all  design={design}  pdk={PDK_NAME}  agent={agent}")
    ddir = design_dir(agent, design)
    # Root-owned: ORFS tree + work_dir (make may create root-owned files there)
    _rmtree_via_docker([
        str(ORFS_FLOW / "results" / PDK_NAME / design),
        str(ORFS_FLOW / "logs"    / PDK_NAME / design),
        str(ORFS_FLOW / "reports" / PDK_NAME / design),
        str(ddir),  # full work_dir for this design/pdk/agent (includes run_logs/)
    ])
    log("all cleaned.")


# ===========================================================================
# MAIN
# ===========================================================================

CLEAN_CHOICES = ["base", "default", "agentic_flow", "all"]
STAGE_CHOICES = ["base", "default", "LLM-iterations",
                 "backend_default", "backend_best",
                 "backend_rank2", "backend_rank3", "backend_rank4",
                 "backend", "all"]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Timing-closure agentic flow — stage-based runner.")
    ap.add_argument("--design",          required=True, help="Design name (e.g. aes)")
    ap.add_argument("--agent",           required=True, help="Agent label (e.g. claude)")
    ap.add_argument("--pdk",             required=True, choices=["asap7", "nangate45"],
                    help="Target PDK")
    ap.add_argument("--clean",           choices=CLEAN_CHOICES,
                    help="Delete data for a stage and exit. Choices: "
                         + ", ".join(CLEAN_CHOICES))
    ap.add_argument("--run-stage",       choices=STAGE_CHOICES, dest="run_stage",
                    help="Run a single stage and exit. Choices: "
                         + ", ".join(STAGE_CHOICES))
    ap.add_argument("--max-iterations",  type=int, default=15,
                    help="Max iterations for LLM-iterations stage (default 15)")
    ap.add_argument("--start-iteration", type=int, default=1,
                    help="Resume LLM-iterations from this iteration (default 1)")
    ap.add_argument("--claude-bin",      default="claude",
                    help="Path to Claude CLI binary (default: claude)")
    args = ap.parse_args()
    if not args.clean and not args.run_stage:
        ap.error("must specify --clean or --run-stage")
    return args


def _dispatch_run_stage(stage: str, agent: str, design: str, args: argparse.Namespace) -> int:
    """Run a single stage. Returns 0 on success, non-zero on failure.

    Wall-time for every stage is appended to runtimes.csv with iteration="-"
    for non-iteration stages; LLM-iterations records per-iteration rows from
    inside the loop."""
    if stage == "LLM-iterations":
        return run_llm_iterations(agent, design, args.claude_bin,
                                   args.max_iterations, args.start_iteration)

    with _time_stage(agent, design, "-", stage):
        if stage == "base":
            run_cts(design, agent)
            prepare_base_artifacts(design, agent)
            return 0
        if stage == "default":
            prepare_default_artifacts(design, agent)
            return 0
        if stage == "backend_default":
            return run_backend_baseline(agent, design)
        if stage == "backend_best":
            return run_backend_rank(agent, design, rank=1)
        if stage.startswith("backend_rank"):
            rank = int(stage.replace("backend_rank", ""))
            return run_backend_rank(agent, design, rank=rank)
    raise ValueError(f"Unknown stage: {stage}")


def main() -> int:
    args = parse_args()
    set_pdk(args.pdk)
    design, agent = args.design, args.agent

    # --- Clean mode ---
    if args.clean:
        if args.clean == "base":         clean_base(agent, design)
        elif args.clean == "default":    clean_default(agent, design)
        elif args.clean == "agentic_flow": clean_agentic_flow(agent, design)
        elif args.clean == "all":        clean_all(agent, design)
        return 0

    # --- Run-stage mode ---
    stage = args.run_stage
    if stage == "all":
        banner(f"RUN ALL STAGES  design={design}  agent={agent}  pdk={args.pdk}")
        for s in ("base", "default", "LLM-iterations",
                  "backend_default", "backend_best",
                  "backend_rank2", "backend_rank3", "backend_rank4"):
            rc = _dispatch_run_stage(s, agent, design, args)
            if rc != 0:
                log(f"[ERROR] Stage '{s}' failed (exit {rc}). Stopping --run-stage all.")
                return rc
        banner("ALL STAGES COMPLETE")
        return 0
    if stage == "backend":
        banner(f"RUN ALL BACKEND STAGES  design={design}  agent={agent}  pdk={args.pdk}")
        for s in ("backend_default", "backend_best",
                  "backend_rank2", "backend_rank3", "backend_rank4"):
            rc = _dispatch_run_stage(s, agent, design, args)
            if rc != 0:
                log(f"[ERROR] Stage '{s}' failed (exit {rc}). Stopping --run-stage backend.")
                return rc
        banner("ALL BACKEND STAGES COMPLETE")
        return 0

    return _dispatch_run_stage(stage, agent, design, args)


if __name__ == "__main__":
    sys.exit(main())
