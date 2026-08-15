# run_default.tcl
#
# Default-stage driver: repair_timing + legalize, reported at placement
# parasitics (no global route). Single-file entry point for --run-stage default.
#
# Sequence:
#   1. Load PDK + base_cts.odb + SDC
#   2. estimate_parasitics -placement (pre-RT baseline)
#   3. repair_timing  (optional SEQUENCE env, else OpenROAD default)
#   4. detailed_placement + check_placement
#   5. estimate_parasitics -placement  (refresh after DPL)
#   6. Placement-parasitic report + full artifacts (generate_design_artifacts.tcl)
#   7. write_db / write_sdc  (HANDOFF)
#
# Required env vars:
#   INPUT_DB, SDC_FILE, OUTPUT_DB, OUTPUT_DIR
#   LEF_DIR, LIB_DIR, SETRC_TCL, PDK_PREAMBLE
#
# Optional env vars:
#   SEQUENCE    — comma-separated repair_timing move list (default: OpenROAD built-in)
#   REPAIR_TNS  — repair_tns percent (default: 100)
#   NWORST      — worst-endpoint path count for generate_design_artifacts (default: 50)
#   STAGE_TAG   — output filename prefix (forced to "default" below)

proc require_env {var} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        error "Required environment variable $var is not set."
    }
    return $::env($var)
}

proc optional_env {var default} {
    if {[info exists ::env($var)] && $::env($var) ne ""} {
        return $::env($var)
    }
    return $default
}

set input_db   [require_env INPUT_DB]
set sdc_file   [require_env SDC_FILE]
set output_db  [require_env OUTPUT_DB]
set output_dir [require_env OUTPUT_DIR]
set setrc_tcl  [require_env SETRC_TCL]

set sequence   [optional_env SEQUENCE ""]
set repair_tns [optional_env REPAIR_TNS 100]

if {![file exists $input_db]} { error "INPUT_DB not found: $input_db" }
if {![file exists $sdc_file]} { error "SDC_FILE not found: $sdc_file" }

file mkdir $output_dir
file mkdir [file dirname $output_db]

# ---------------------------------------------------------------------------
# 1. Load PDK + design
# ---------------------------------------------------------------------------
# NOTE: read_db MUST come before the PDK preamble's read_lef. The saved .odb
# already carries the tech/standard-cell LEF; issuing read_lef first and then
# read_db duplicates the cell masters, which corrupts the resizer's buffer list
# and SIGSEGVs (dbMaster::getSite) inside repair_timing on current OpenROAD
# builds. Loading the DB first, then the liberty, avoids the duplicate masters.
read_db  $input_db
source [require_env PDK_PREAMBLE]
read_sdc $sdc_file
source   $setrc_tcl

# Don't-use list — mirror of ORFS asap7 config.mk DONT_USE_CELLS. Applied AFTER
# read_db (set_dont_use needs a linked design) and BEFORE repair_timing, so the
# resizer cannot size into the forbidden fractional-drive cells. The ORFS
# DONT_USE_CELLS env var is not propagated to this standalone OpenROAD process,
# so the patterns are hardcoded here (ASAP7-specific; harmless no-op on others).
set_dont_use { *x1p*_ASAP7* *xp*_ASAP7* SDF* ICG* }

# Pre-RT placement parasitics (for before-WNS baseline).
estimate_parasitics -placement
set before_wns [sta::worst_slack -max]
set before_tns [sta::total_negative_slack -max]

# ---------------------------------------------------------------------------
# 2. repair_timing
# ---------------------------------------------------------------------------
if {$sequence eq ""} {
    puts "Running default repair_timing (built-in sequence)"
    repair_timing -setup -repair_tns $repair_tns
} else {
    puts "Running repair_timing with sequence: $sequence"
    repair_timing -setup -sequence $sequence -repair_tns $repair_tns
}

# ---------------------------------------------------------------------------
# 3. Legalize + refresh placement parasitics
# ---------------------------------------------------------------------------
detailed_placement
check_placement -verbose
estimate_parasitics -placement

# ---------------------------------------------------------------------------
# 4. Pre-GR small summary (placement parasitics): WNS/TNS/area/util/power
# ---------------------------------------------------------------------------
set pre_gr_wns [sta::worst_slack -max]
set pre_gr_tns [sta::total_negative_slack -max]

sta::redirect_string_begin
report_design_area
set pre_gr_area_rpt [sta::redirect_string_end]

sta::redirect_string_begin
report_power -digits 3
set pre_gr_power_rpt [sta::redirect_string_end]

set pre_gr_area ""
set pre_gr_util ""
if {[regexp {Design area ([0-9.eE+-]+) um\^2 ([0-9.eE+-]+)% utilization} \
        $pre_gr_area_rpt -> av uv]} {
    set pre_gr_area $av
    set pre_gr_util $uv
}
set pre_gr_power ""
foreach line [split $pre_gr_power_rpt "\n"] {
    if {[regexp {^Total\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)} \
            $line -> p_int p_switch p_leak p_total]} {
        set pre_gr_power $p_total
        break
    }
}

set pregr_file [file join $output_dir default_placement_summary.txt]
set fh [open $pregr_file w]
puts $fh "Pre-GR (placement-parasitic) summary — post-repair_timing, post-DPL"
puts $fh [format "WNS:         %.6f" $pre_gr_wns]
puts $fh [format "TNS:         %.6f" $pre_gr_tns]
puts $fh [format "Area:        %s um^2" $pre_gr_area]
puts $fh [format "Utilization: %s%%"    $pre_gr_util]
puts $fh [format "Power:       %s W"    $pre_gr_power]
close $fh
puts "Pre-GR summary: $pregr_file"
puts [format "Pre-GR  WNS=%.6f  TNS=%.6f  Area=%s um^2  Util=%s%%  Power=%s W" \
    $pre_gr_wns $pre_gr_tns $pre_gr_area $pre_gr_util $pre_gr_power]

# ---------------------------------------------------------------------------
# 4b. Placement report — captured at POST-LEGALIZATION (placement parasitics).
#     Source generate_placement_report.tcl in-session so it uses the current
#     post-DPL state. STAGE_TAG forced to "default" so file is default_*.
# ---------------------------------------------------------------------------
set ::env(STAGE_TAG) "default"
set placement_report_skip_load 1
source [file join [file dirname [info script]] generate_placement_report.tcl]

# ---------------------------------------------------------------------------
# 4c. Report timing + metrics at PLACEMENT parasitics (post-repair_timing,
#     post-DPL). Sourced with skip_load=1 so it reports the current
#     placement-parasitic state. These are the reported default_* artifacts:
#     default_timing.txt, default_metrics.csv, default_worst_paths.txt,
#     default_area.rpt, default_power.rpt, violating_endpoint.txt.
# ---------------------------------------------------------------------------
set generate_design_artifacts_skip_load 1
source [file join [file dirname [info script]] generate_design_artifacts.tcl]

# ---------------------------------------------------------------------------
# 5. HANDOFF — save pre-GR DB + SDC. The saved ODB is the seed for the next
#    iteration (LLM loop) and the clean handoff to the backend.
# ---------------------------------------------------------------------------
write_db  $output_db
write_sdc [file join $output_dir constraint.sdc]
puts "Handoff DB written: $output_db"

puts "Default stage complete."
puts [format "  WNS=%.6f  TNS=%.6f (placement parasitics, post-repair_timing)" $pre_gr_wns $pre_gr_tns]
puts "  Handoff ODB: $output_db"
