"""Pure-Python Executor: validate Planner decision, generate run_plan.tcl files.

Reads planner_decision.json from the iteration directory, validates it, and
emits one run_plan.tcl per plan into planX/ subdirs.

Does NOT invoke Docker or OpenROAD — launch_workers.py handles that after this
module returns.

Public entry points:
    build_plans(iter_dir)             -> (ok: bool, errors: list[str])
    fix_tcl_errors(failures, iter_dir) -> (fixed_count: int, unfixable: list[str])
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Dict, List, Tuple

VALID_MOVES = {
    "sizeup", "sizedown", "vt_swap", "buffer",
    "split", "unbuffer", "clone", "sizeup_match", "swap",
}
MAX_SEQ_LEN = 9
VALID_ECO_ACTIONS = {"resize", "delete_buffer", "insert_buffer"}

# Instance name prefixes that identify clock tree cells inserted by CTS.
# ECO actions on these cells change clock skew on ALL endpoints simultaneously
# and invalidate the CTS result — they must never be targeted by the Planner.
_CLOCK_TREE_PREFIXES = (
    "clkbuf_", "clknet_", "clkgate_", "cts_", "clkinv_",
    "clkload_", "clkmux_", "clkskew_",
)

def _is_clock_tree_inst(inst: str) -> bool:
    """Return True if the instance name matches a CTS-inserted clock tree cell."""
    low = inst.lower()
    return any(low.startswith(p) for p in _CLOCK_TREE_PREFIXES)


# Sequential cell detection — DFFs and latches must not be targeted by ECO.
# Repair of sequential cells (resizing FFs) changes setup/hold timing behaviour
# and Q-to-output delay in ways that affect all paths through that FF.
# Only combinational cells between FFs should be targeted.
_SEQ_MASTER_PREFIXES = (
    "dff", "dffe", "dffa", "dffr", "dffs",   # standard DFF variants
    "latch", "dlatch", "sdff",                 # latches and scan FFs
)
_SEQ_INST_SUBSTRINGS = (
    "$_dff", "$_dffe", "$_dffsr", "$_dlatch",  # yosys-mapped sequential cells
)

def _is_sequential_cell(inst: str, master: str = "") -> bool:
    """Return True if inst or master indicates a flip-flop or latch."""
    inst_low   = inst.lower()
    master_low = master.lower()
    if any(s in inst_low for s in _SEQ_INST_SUBSTRINGS):
        return True
    if any(master_low.startswith(p) for p in _SEQ_MASTER_PREFIXES):
        return True
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_run_knobs(knobs: dict, ctx: str) -> List[str]:
    errs: List[str] = []
    allowed = {
        "repair_tns", "max_passes", "max_iterations",
        "setup_margin",
        "skip_last_gasp", "skip_crit_vt_swap",
    }
    for k in knobs:
        if k not in allowed:
            errs.append(f"{ctx}: unknown knob '{k}'")
    return errs


def _validate_plan(plan: dict, idx: int) -> List[str]:
    errs: List[str] = []
    prefix = f"plan{idx}"
    ptype = plan.get("plan_type")
    if ptype not in ("sequence", "staged", "eco"):
        errs.append(f"{prefix}: invalid plan_type '{ptype}'")
        return errs

    if ptype == "sequence":
        seq = plan.get("sequence")
        if not isinstance(seq, list) or not seq:
            errs.append(f"{prefix}: sequence must be a non-empty list")
            return errs
        if len(seq) > MAX_SEQ_LEN:
            errs.append(f"{prefix}: sequence length {len(seq)} exceeds max {MAX_SEQ_LEN}")
        if len(set(seq)) != len(seq):
            errs.append(f"{prefix}: sequence contains duplicates — each move at most once")
        for m in seq:
            if m not in VALID_MOVES:
                errs.append(f"{prefix}: invalid move '{m}'")
        errs += _validate_run_knobs(plan.get("run_knobs") or {}, f"{prefix}.run_knobs")

    elif ptype == "staged":
        stages = plan.get("stages")
        if not isinstance(stages, list) or len(stages) < 2:
            errs.append(f"{prefix}: staged plan needs >=2 stages")
            return errs
        if len(stages) > MAX_SEQ_LEN:
            errs.append(f"{prefix}: too many stages (>{MAX_SEQ_LEN})")
        for si, st in enumerate(stages, 1):
            m = st.get("move")
            if m not in VALID_MOVES:
                errs.append(f"{prefix}.stage{si}: invalid move '{m}'")
            errs += _validate_run_knobs(st.get("run_knobs") or {},
                                        f"{prefix}.stage{si}.run_knobs")

    elif ptype == "eco":
        changes = plan.get("changes")
        if not isinstance(changes, list) or not changes:
            errs.append(f"{prefix}: eco plan needs non-empty changes")
            return errs
        if len(changes) > 20:
            errs.append(f"{prefix}: eco plan has {len(changes)} changes (max 20)")
        for ci, ch in enumerate(changes, 1):
            act = ch.get("action")
            if act not in VALID_ECO_ACTIONS:
                errs.append(f"{prefix}.change{ci}: invalid action '{act}'")
                continue
            if act == "resize":
                if not ch.get("inst") or not ch.get("new_master"):
                    errs.append(f"{prefix}.change{ci}: resize needs inst + new_master")
                elif _is_clock_tree_inst(ch["inst"]):
                    errs.append(
                        f"{prefix}.change{ci}: BLOCKED — '{ch['inst']}' is a clock tree "
                        f"cell (CTS-inserted). Modifying clock cells changes skew on ALL "
                        f"endpoints and invalidates CTS. Only target data path instances."
                    )
                elif _is_sequential_cell(ch["inst"], ch.get("new_master", "")):
                    errs.append(
                        f"{prefix}.change{ci}: BLOCKED — '{ch['inst']}' is a sequential "
                        f"cell (DFF/latch). ECO resize must only target combinational cells. "
                        f"DFF resizing changes Q-to-output delay and setup/hold constraints "
                        f"on ALL paths through that register."
                    )
            elif act == "delete_buffer":
                if not ch.get("inst"):
                    errs.append(f"{prefix}.change{ci}: delete_buffer needs inst")
                elif _is_clock_tree_inst(ch["inst"]):
                    errs.append(
                        f"{prefix}.change{ci}: BLOCKED — '{ch['inst']}' is a clock tree "
                        f"cell. Do not remove CTS-inserted buffers."
                    )
                elif _is_sequential_cell(ch["inst"]):
                    errs.append(
                        f"{prefix}.change{ci}: BLOCKED — '{ch['inst']}' appears to be a "
                        f"sequential cell. delete_buffer only applies to combinational buffers."
                    )
            elif act == "insert_buffer":
                for fld in ("net", "driver_pin", "buf_cell", "buf_name"):
                    if not ch.get(fld):
                        errs.append(f"{prefix}.change{ci}: insert_buffer needs '{fld}'")
    return errs


def validate_decision(decision: dict) -> List[str]:
    errs: List[str] = []
    if decision.get("decision") != "execute":
        errs.append(f"decision must be 'execute', got '{decision.get('decision')}'")
        return errs
    plans = decision.get("plans") or []
    if not plans:
        errs.append("no plans in decision")
        return errs
    pc = decision.get("plan_count")
    if pc is not None and pc != len(plans):
        errs.append(f"plan_count={pc} mismatches plans length={len(plans)}")
    for i, plan in enumerate(plans, 1):
        errs += _validate_plan(plan, i)
    return errs


# ---------------------------------------------------------------------------
# TCL generation
# ---------------------------------------------------------------------------

_PREAMBLE = """\
# Auto-generated by Executor — do not edit manually
# Plan: {ptype}, Iteration: {it}, Plan index: {idx}

# --- Load design + technology ---
# read_db MUST precede the preamble's read_lef: the saved .odb already carries
# the tech/cell LEF, so read_lef-then-read_db duplicates the cell masters and
# SIGSEGVs (dbMaster::getSite) in the resizer's buffer list during repair_timing
# on current OpenROAD builds. Load the DB first, then liberty.
read_db  $env(INPUT_DB)
source $env(PDK_PREAMBLE)
read_sdc $env(SDC_FILE)
source   $env(SETRC_TCL)

# Don't-use list — mirror of ORFS asap7 config.mk DONT_USE_CELLS. Applied AFTER
# read_db (set_dont_use needs a linked design) and BEFORE repair_timing so the
# resizer cannot size into forbidden fractional-drive cells. The ORFS
# DONT_USE_CELLS env var is not propagated to this standalone OpenROAD process.
set_dont_use {{ *x1p*_ASAP7* *xp*_ASAP7* SDF* ICG* }}
"""

_PARASITIC_LINE = "estimate_parasitics -placement\n"

# The postamble is load-bearing — order must be preserved exactly:
#   1. DPL + placement report (POST-LEGALIZATION, placement parasitics)
#   2. write_db + write_sdc  (PRE-GR handoff — seed for next iteration)
#   3. GR Blocks A–D         (measurement only; not persisted to ODB)
#   4. generate_design_artifacts (POST-GR timing/area/power/paths)
_POSTAMBLE = """\

# --- Legalize and re-estimate parasitics ---
detailed_placement
check_placement -verbose
estimate_parasitics -placement

# --- Placement report (POST-LEGALIZATION, BEFORE GR) ---
set placement_report_skip_load 1
source scripts/tcl/generate_placement_report.tcl

# --- Write HANDOFF database (PRE-GR state) ---
write_db  $env(OUTPUT_DB)
write_sdc [file join $env(OUTPUT_DIR) output.sdc]

# --- Global route (Blocks A-D) ---
# Block A — optional pre-GR hook
if {[info exists ::env(PRE_GLOBAL_ROUTE_TCL)] && [file exists $::env(PRE_GLOBAL_ROUTE_TCL)]} {
    source $::env(PRE_GLOBAL_ROUTE_TCL)
}
set _res_aware {}
if {[info exists ::env(ENABLE_RESISTANCE_AWARE)] && $::env(ENABLE_RESISTANCE_AWARE) ne ""} {
    set _res_aware [list -resistance_aware 0]
}

# Configure routing layers — must be set after every read_db/write_db
if {[info exists ::env(MIN_ROUTING_LAYER)] && $::env(MIN_ROUTING_LAYER) ne "" \
    && [info exists ::env(MAX_ROUTING_LAYER)] && $::env(MAX_ROUTING_LAYER) ne ""} {
    set _layer_range "$::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)"
    set_routing_layers -signal $_layer_range
    set _adj "0.5"
    if {[info exists ::env(ROUTING_LAYER_ADJUSTMENT)] && $::env(ROUTING_LAYER_ADJUSTMENT) ne ""} {
        set _adj $::env(ROUTING_LAYER_ADJUSTMENT)
    }
    set_global_routing_layer_adjustment $_layer_range $_adj
}

# Block B — pin access
pin_access

# Block C — global_route (catch GRT-0116 on congested designs)
set _gr_extra {}
if {[info exists ::env(GLOBAL_ROUTE_ARGS)] && $::env(GLOBAL_ROUTE_ARGS) ne ""} {
    set _gr_extra $::env(GLOBAL_ROUTE_ARGS)
}
if {[catch {
    global_route \\
        -congestion_report_file [file join $env(OUTPUT_DIR) congestion.rpt] \\
        -verbose \\
        {*}$_gr_extra {*}$_res_aware
} _gr_err]} {
    puts "WARNING: global_route raised: $_gr_err"
    puts "WARNING: continuing with measurement — guides still usable."
}

# Block D — propagated clock + GR-aware parasitics
if {[info exists ::env(CELL_PAD_IN_SITES_DETAIL_PLACEMENT)] && $::env(CELL_PAD_IN_SITES_DETAIL_PLACEMENT) ne ""} {
    set_placement_padding -global \\
        -left  $::env(CELL_PAD_IN_SITES_DETAIL_PLACEMENT) \\
        -right $::env(CELL_PAD_IN_SITES_DETAIL_PLACEMENT)
}
set_propagated_clock [all_clocks]
estimate_parasitics -global_routing

# --- Extract artifacts (POST-GR) ---
set generate_design_artifacts_skip_load 1
source scripts/tcl/generate_design_artifacts.tcl
"""


def _emit_knob_flags(knobs: dict, skip_finishing: bool = False) -> List[str]:
    flags: List[str] = []
    if "repair_tns" in knobs:
        flags.append(f"-repair_tns {knobs['repair_tns']}")
    if "max_passes" in knobs:
        flags.append(f"-max_passes {knobs['max_passes']}")
    if "max_iterations" in knobs:
        flags.append(f"-max_iterations {knobs['max_iterations']}")
    # max_repairs_per_pass > 1 triggers a SIGSEGV in SizeUpMove::doMove on this
    # OpenROAD build — always force to 1 regardless of what the planner requested.
    flags.append("-max_repairs_per_pass 1")
    if "setup_margin" in knobs:
        flags.append(f"-setup_margin {knobs['setup_margin']}")
    if skip_finishing or knobs.get("skip_last_gasp"):
        flags.append("-skip_last_gasp")
    if skip_finishing or knobs.get("skip_crit_vt_swap"):
        flags.append("-skip_crit_vt_swap")
    return flags


def _format_repair_timing(sequence_str: str, knobs: dict,
                           skip_finishing: bool = False) -> str:
    lines = ["repair_timing \\",
             "    -setup \\",
             f"    -sequence {sequence_str} \\"]
    flags = _emit_knob_flags(knobs, skip_finishing=skip_finishing)
    for i, f in enumerate(flags):
        cont = " \\" if i < len(flags) - 1 else ""
        lines.append(f"    {f}{cont}")
    if not flags:
        lines[-1] = lines[-1].rstrip(" \\")
    return "\n".join(lines) + "\n"


def _sequence_tcl(plan: dict, it: int, idx: int) -> str:
    out = _PREAMBLE.format(ptype="sequence", it=it, idx=idx)
    out += _PARASITIC_LINE
    out += "\n# --- repair_timing ---\n"
    seq_str = ",".join(plan["sequence"])
    out += _format_repair_timing(seq_str, plan.get("run_knobs") or {})
    out += _POSTAMBLE
    return out


def _staged_tcl(plan: dict, it: int, idx: int) -> str:
    out = _PREAMBLE.format(ptype="staged", it=it, idx=idx)
    out += _PARASITIC_LINE
    stages = plan["stages"]
    for si, st in enumerate(stages, 1):
        final = (si == len(stages))
        label = st.get("label", f"stage {si}")
        tag = "FINAL" if final else "non-final"
        out += f"\n# --- Stage {si} ({tag}): {label} ---\n"
        out += _format_repair_timing(st["move"], st.get("run_knobs") or {},
                                     skip_finishing=not final)
        if not final:
            out += "\ndetailed_placement\nestimate_parasitics -placement\n"
    out += _POSTAMBLE
    return out


def _eco_tcl(plan: dict, it: int, idx: int) -> str:
    # ECO plans skip estimate_parasitics -placement before changes
    # (replace_cell invalidates parasitic state; postamble re-estimates after DPL)
    changes = plan["changes"]
    needs_buf_insert = any(ch["action"] == "insert_buffer" for ch in changes)

    out = _PREAMBLE.format(ptype="eco", it=it, idx=idx)
    if needs_buf_insert:
        out += "\n# --- Load eco_insert_buffer proc ---\n"
        out += "source scripts/tcl/eco_buf_insert.tcl\n"
    out += "\n# --- Directed ECO changes ---\n"
    for ch in changes:
        act = ch["action"]
        if act == "resize":
            out += f"replace_cell {{{ch['inst']}}} {ch['new_master']}\n"
        elif act == "delete_buffer":
            out += f"remove_buffers [get_cells {{{ch['inst']}}}]\n"
        elif act == "insert_buffer":
            sinks = ch.get("sinks", "all")
            at    = ch.get("at", "centroid")
            in_p  = ch.get("in_pin", "A")
            out_p = ch.get("out_pin", "Y")
            sinks_arg = (f"-sinks {{{' '.join(sinks)}}}" if isinstance(sinks, list)
                         else "")
            out += (f"eco_insert_buffer {{{ch['net']}}} {{{ch['driver_pin']}}} "
                    f"{ch['buf_cell']} {ch['buf_name']} "
                    f"-at {at} -in_pin {in_p} -out_pin {out_p} {sinks_arg}\n")
    out += _POSTAMBLE
    return out


def generate_run_plan_tcl(plan: dict, iteration: int, plan_index: int) -> str:
    ptype = plan.get("plan_type", "sequence")
    if ptype == "sequence":
        return _sequence_tcl(plan, iteration, plan_index)
    if ptype == "staged":
        return _staged_tcl(plan, iteration, plan_index)
    if ptype == "eco":
        return _eco_tcl(plan, iteration, plan_index)
    raise ValueError(f"unknown plan_type: {ptype}")


# ---------------------------------------------------------------------------
# Build plans — main entry point
# ---------------------------------------------------------------------------

def build_plans(iter_dir: pathlib.Path) -> Tuple[bool, List[str]]:
    """Read planner_decision.json, validate, write run_plan.tcl for every plan.

    Invalid individual plans are skipped with a warning; the iteration continues
    with the remaining valid plans.  Only returns False when the JSON is
    structurally broken or every plan fails validation (nothing left to run).
    """
    dec_path = iter_dir / "planner_decision.json"
    if not dec_path.exists():
        return False, [f"planner_decision.json not found at {dec_path}"]
    try:
        decision = json.loads(dec_path.read_text())
    except json.JSONDecodeError as e:
        return False, [f"planner_decision.json is invalid JSON: {e}"]

    # Structural-level checks (decision field, empty plans list)
    if decision.get("decision") != "execute":
        return False, [f"decision must be 'execute', got '{decision.get('decision')}'"]
    plans = decision.get("plans") or []
    if not plans:
        return False, ["no plans in decision"]

    iter_n   = int(iter_dir.name.replace("iteration", ""))
    warnings: List[str] = []
    built    = 0

    for i, plan in enumerate(plans, 1):
        plan_errs = _validate_plan(plan, i)
        if plan_errs:
            warnings.append(f"plan{i} skipped (invalid): {'; '.join(plan_errs)}")
            continue
        plan_dir = iter_dir / f"plan{i}"
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "run_plan.tcl").write_text(
            generate_run_plan_tcl(plan, iter_n, i)
        )
        built += 1

    if built == 0:
        return False, warnings or ["all plans failed validation"]
    return True, warnings


# ---------------------------------------------------------------------------
# Retry — deterministic fixes for known TCL-error patterns
# ---------------------------------------------------------------------------

_TCL_FIXES: List[Tuple[str, str, str]] = [
    ("invalid command name \"resize_cell\"", r"\bresize_cell\b",  "replace_cell"),
    ("invalid command name \"unbuffer\"",    r"\bunbuffer\b ",    "remove_buffers "),
]


def _fix_braced_sequence(tcl: str) -> Tuple[str, bool]:
    """Rewrite `-sequence { a b c }` → `-sequence a,b,c`."""
    pat = re.compile(r"-sequence\s*\{([^}]+)\}")
    def repl(m: re.Match) -> str:
        toks = [t for t in re.split(r"\s+", m.group(1).strip()) if t]
        return f"-sequence {','.join(toks)}"
    new = pat.sub(repl, tcl)
    return new, (new != tcl)


def fix_tcl_errors(failures: List[Dict],
                   iter_dir: pathlib.Path) -> Tuple[int, List[str]]:
    """For each failed plan, apply deterministic rewrites to run_plan.tcl.
    Returns (fixed_count, unfixable_descriptions)."""
    fixed = 0
    unfixable: List[str] = []
    for f in failures:
        plan = f.get("plan", "")
        err  = f.get("first_error", "") or ""
        tcl_path = iter_dir / plan / "run_plan.tcl"
        if not tcl_path.exists():
            unfixable.append(f"{plan}: run_plan.tcl missing")
            continue
        src = tcl_path.read_text()
        new = src
        applied: List[str] = []

        for err_sub, pat, repl in _TCL_FIXES:
            if err_sub in err:
                new2 = re.sub(pat, repl, new)
                if new2 != new:
                    applied.append(err_sub)
                    new = new2

        new, braced = _fix_braced_sequence(new)
        if braced:
            applied.append("braced-sequence")

        if new != src:
            tcl_path.write_text(new)
            fixed += 1
        else:
            unfixable.append(f"{plan}: no known fix for: {err[:120]}")
    return fixed, unfixable
