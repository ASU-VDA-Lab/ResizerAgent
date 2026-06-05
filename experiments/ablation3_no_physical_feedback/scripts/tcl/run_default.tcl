# run_default.tcl
#
# Default-stage driver: repair_timing + legalize → measure at placement
# parasitics → write handoff DB + reports. No global route in the loop;
# GR runs in the backend stage instead. Single-file entry point for
# --run-stage default.
#
# Sequence:
#   1. Load PDK + base_cts.odb + SDC
#   2. estimate_parasitics -placement (pre-RT baseline)
#   3. repair_timing  (optional SEQUENCE env, else OpenROAD default)
#   4. detailed_placement + check_placement
#   5. estimate_parasitics -placement  (refresh after DPL)
#   6. Small placement-parasitic summary: WNS/TNS/area/util/power
#   7. Placement report (post-DPL spatial / critical-path layout)
#   8. write_db / write_sdc                                ← HANDOFF
#   9. Full at-placement artifacts via generate_design_artifacts.tcl
#      (sourced in-session with skip_load=1, so no re-read)
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
source [require_env PDK_PREAMBLE]
read_db  $input_db
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

set summary_file [file join $output_dir default_placement_summary.txt]
set fh [open $summary_file w]
puts $fh "Placement-parasitic summary — post-repair_timing, post-DPL"
puts $fh [format "WNS:         %.6f" $pre_gr_wns]
puts $fh [format "TNS:         %.6f" $pre_gr_tns]
puts $fh [format "Area:        %s um^2" $pre_gr_area]
puts $fh [format "Utilization: %s%%"    $pre_gr_util]
puts $fh [format "Power:       %s W"    $pre_gr_power]
close $fh
puts "Placement summary: $summary_file"
puts [format "Placement WNS=%.6f  TNS=%.6f  Area=%s um^2  Util=%s%%  Power=%s W" \
    $pre_gr_wns $pre_gr_tns $pre_gr_area $pre_gr_util $pre_gr_power]

# ---------------------------------------------------------------------------
# 5. HANDOFF — save the at-placement DB + SDC. This is the seed for the next
#    iteration (LLM loop) and the input to the backend stage. Global route
#    runs in the backend; no GR state is written here.
# ---------------------------------------------------------------------------
write_db  $output_db
write_sdc [file join $output_dir constraint.sdc]
puts "Handoff DB written (at-placement): $output_db"

# ---------------------------------------------------------------------------
# 6. Full at-placement artifacts — delegate to generate_design_artifacts.tcl
#    with skip_load=1 so it uses the current in-session placement parasitics.
# ---------------------------------------------------------------------------
set generate_design_artifacts_skip_load 1
source [file join [file dirname [info script]] generate_design_artifacts.tcl]

puts "Default stage complete."
puts [format "  Placement WNS=%.6f  TNS=%.6f" $pre_gr_wns $pre_gr_tns]
puts "  Handoff ODB: $output_db (at-placement state)"
