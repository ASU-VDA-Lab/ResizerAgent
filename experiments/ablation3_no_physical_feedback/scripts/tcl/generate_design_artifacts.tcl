# generate_design_artifacts.tcl
#
# Generate timing/area/power/path artifacts from a given ODB.
# All configuration is via environment variables — no sidecar files.
#
# Required:
#   INPUT_DB    - path to input .odb file
#   SDC_FILE    - path to .sdc constraints file
#   OUTPUT_DIR  - directory to write all artifacts into
#   LEF_DIR     - directory containing ASAP7 .lef files
#   LIB_DIR     - directory containing ASAP7 .lib files
#   SETRC_TCL   - path to platform setRC.tcl
#
# Optional:
#   STAGE_TAG   - label prefix for output filenames (default: base)
#   NWORST      - number of worst endpoints to report (default: 50); 5 paths per endpoint are captured

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
set output_dir [require_env OUTPUT_DIR]
set lef_dir    [require_env LEF_DIR]
set lib_dir    [require_env LIB_DIR]
set setrc_tcl  [require_env SETRC_TCL]

set stage_tag  [optional_env STAGE_TAG "default"]
set nworst     [optional_env NWORST 50]

file mkdir $output_dir

# --- Load libraries and design ---
# Skipped when sourced from within an existing OpenROAD session (design already loaded).
# Set generate_design_artifacts_skip_load 1 before sourcing to skip this block.
if {![info exists generate_design_artifacts_skip_load] || !$generate_design_artifacts_skip_load} {
    if {![file exists $input_db]} { error "INPUT_DB not found: $input_db" }
    if {![file exists $sdc_file]}  { error "SDC_FILE not found: $sdc_file" }

    # PDK-specific LEF + Liberty (platforms/<pdk>/preamble.tcl)
    source [require_env PDK_PREAMBLE]

    puts "Reading DB: $input_db"
    read_db $input_db
    puts "Reading SDC: $sdc_file"
    read_sdc $sdc_file
    source $setrc_tcl
    # Standalone path: assumes caller passes an at-placement ODB (default.odb,
    # plan<i>/output.odb). The loop runs no GR; reports are at placement
    # parasitics. For raw post-CTS ODBs with no repair_timing applied, use
    # generate_base_artifacts.tcl instead.
    set_propagated_clock [all_clocks]
    estimate_parasitics -placement
}

set wns [sta::worst_slack -max]
set tns [sta::total_negative_slack -max]

sta::redirect_string_begin
report_checks -path_delay max -format full_clock_expanded \
    -fields {slew capacitance input_pin net} -digits 4 \
    -endpoint_path_count 5 -group_path_count [expr {$nworst * 5}] \
    -sort_by_slack -slack_max 0.0
set worst_paths_report [sta::redirect_string_end]

sta::redirect_string_begin
report_design_area
set area_report [sta::redirect_string_end]

sta::redirect_string_begin
report_power -digits 3
set power_report [sta::redirect_string_end]

# Count all violating endpoints
set violating_endpoints 0
if {[catch {
    sta::redirect_string_begin
    report_checks -path_delay max -format full_clock_expanded \
        -fields {slew capacitance input_pin net} -digits 4 \
        -endpoint_path_count 1 -group_path_count 100000 \
        -sort_by_slack -slack_max 0.0
    set viol_rpt [sta::redirect_string_end]
    set violating_endpoints [regexp -all -line {^Startpoint:} $viol_rpt]
} err]} {
    puts "Warning: failed to count violating endpoints: $err"
}

set area_um2 ""
set util_percent ""
if {[regexp {Design area ([0-9.eE+-]+) um\^2 ([0-9.eE+-]+)% utilization\.} $area_report -> area_val util_val]} {
    set area_um2 $area_val
    set util_percent $util_val
}

set total_power ""
foreach line [split $power_report "\n"] {
    if {[regexp {^Total\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)} $line -> p_int p_switch p_leak p_total]} {
        set total_power $p_total
        break
    }
}

set timing_file  [file join $output_dir ${stage_tag}_timing.txt]
set worst_file   [file join $output_dir ${stage_tag}_worst_paths.txt]
set area_file    [file join $output_dir ${stage_tag}_area.rpt]
set power_file   [file join $output_dir ${stage_tag}_power.rpt]
set netlist_file [file join $output_dir ${stage_tag}_netlist.v]
set viol_file    [file join $output_dir violating_endpoint.txt]
set metrics_csv  [file join $output_dir ${stage_tag}_metrics.csv]

set fh [open $timing_file w]
puts $fh [format "WNS: %.6f\nTNS: %.6f" $wns $tns]
close $fh

set fh [open $worst_file w]
puts $fh $worst_paths_report
close $fh

set fh [open $area_file w]
puts $fh $area_report
close $fh

set fh [open $power_file w]
puts $fh $power_report
close $fh

set fh [open $viol_file w]
puts $fh $violating_endpoints
close $fh

write_verilog $netlist_file

set need_header [expr {![file exists $metrics_csv]}]
set fh [open $metrics_csv a+]
if {$need_header} {
    puts $fh "stage,wns,tns,area_um2,util_percent,power_mw,violating_endpoints,input_db,sdc_file"
}
puts $fh [format {"%s",%.6f,%.6f,%s,%s,%s,%d,"%s","%s"} \
    $stage_tag $wns $tns $area_um2 $util_percent $total_power \
    $violating_endpoints $input_db $sdc_file]
close $fh

puts "Generated artifacts in: $output_dir"
puts "  $timing_file"
puts "  $worst_file"
puts "  $area_file"
puts "  $power_file"
puts "  $viol_file"
puts "  $netlist_file"
puts "  $metrics_csv"
