# generate_placement_report.tcl
#
# Generate a placement/floorplan report for LLM planner context.
# Reports die dimensions, spatial utilization grid, critical path cell
# locations with local density, worst-path bounding box, and free row
# slot count.  All configuration via environment variables.
#
# Required:
#   INPUT_DB    - path to input .odb file
#   SDC_FILE    - path to .sdc constraints file
#   OUTPUT_DIR  - directory to write the report into
#   LEF_DIR     - directory containing ASAP7 .lef files
#   LIB_DIR     - directory containing ASAP7 .lib files
#   SETRC_TCL   - path to platform setRC.tcl
#
# Optional:
#   STAGE_TAG       - label prefix for output filenames (default: base)
#   GRID_ROWS       - utilization grid rows (default: 5)
#   GRID_COLS       - utilization grid columns (default: 5)
#   DENSITY_RADIUS  - local-density half-window in um (default: 20)
#   NWORST_PATHS    - number of worst paths to extract for bounding box (default: 5)

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

set stage_tag      [optional_env STAGE_TAG "default"]
set grid_rows      [optional_env GRID_ROWS 5]
set grid_cols      [optional_env GRID_COLS 5]
set density_radius [optional_env DENSITY_RADIUS 20]
set nworst_paths   [optional_env NWORST_PATHS 5]

file mkdir $output_dir

# --- Load libraries and design ---
# Skipped when sourced from within an existing OpenROAD session (design already loaded).
# Set placement_report_skip_load 1 before sourcing to skip this block.
if {![info exists placement_report_skip_load] || !$placement_report_skip_load} {
    if {![file exists $input_db]} { error "INPUT_DB not found: $input_db" }
    if {![file exists $sdc_file]}  { error "SDC_FILE not found: $sdc_file" }

    # PDK-specific LEF + Liberty (platforms/<pdk>/preamble.tcl)
    source [require_env PDK_PREAMBLE]

    puts "Reading DB: $input_db"
    read_db $input_db
    puts "Reading SDC: $sdc_file"
    read_sdc $sdc_file
    source $setrc_tcl
    # Standalone path: assumes caller passes an after-GR ODB. For raw
    # post-CTS ODBs, use generate_base_artifacts.tcl instead.
    set_propagated_clock [all_clocks]
    estimate_parasitics -global_routing
}

# ===========================================================================
# 1. Die and core dimensions
# ===========================================================================
set db   [ord::get_db]
set chip [$db getChip]
set blk  [$chip getBlock]
set die  [$blk getDieArea]

# OpenROAD coordinates are in DBU; 1 um = 1000 DBU for ASAP7
set dbu_per_um 1000.0

set die_x0 [expr {[$die xMin] / $dbu_per_um}]
set die_y0 [expr {[$die yMin] / $dbu_per_um}]
set die_x1 [expr {[$die xMax] / $dbu_per_um}]
set die_y1 [expr {[$die yMax] / $dbu_per_um}]
set die_w  [expr {$die_x1 - $die_x0}]
set die_h  [expr {$die_y1 - $die_y0}]

# Core area (first row group bounding box, or use die if core not set)
set core [$blk getCoreArea]
set core_x0 [expr {[$core xMin] / $dbu_per_um}]
set core_y0 [expr {[$core yMin] / $dbu_per_um}]
set core_x1 [expr {[$core xMax] / $dbu_per_um}]
set core_y1 [expr {[$core yMax] / $dbu_per_um}]
set core_w  [expr {$core_x1 - $core_x0}]
set core_h  [expr {$core_y1 - $core_y0}]

puts "Die: ${die_w}x${die_h} um  Core: ${core_w}x${core_h} um"

# ===========================================================================
# 2. Collect all placed cell locations and areas
# ===========================================================================
# cell_x/y in um; cell_area in um^2; cell_name -> list {x y area}
array set cell_info {}
set total_placed 0
set total_cell_area 0.0

foreach inst [$blk getInsts] {
    if {[$inst isPlaced] == 0} continue
    set master [$inst getMaster]
    set bbox   [$inst getBBox]
    set cx [expr {([$bbox xMin] + [$bbox xMax]) / 2.0 / $dbu_per_um}]
    set cy [expr {([$bbox yMin] + [$bbox yMax]) / 2.0 / $dbu_per_um}]
    set w  [expr {[$master getWidth]  / $dbu_per_um}]
    set h  [expr {[$master getHeight] / $dbu_per_um}]
    set a  [expr {$w * $h}]
    set cell_info([$inst getName]) [list $cx $cy $a]
    set total_placed [expr {$total_placed + 1}]
    set total_cell_area [expr {$total_cell_area + $a}]
}

puts "Placed cells: $total_placed  Total cell area: [format %.2f $total_cell_area] um^2"

# ===========================================================================
# 3. Spatial utilization grid  (grid_rows x grid_cols)
# ===========================================================================
# Each grid cell tracks sum of placed cell area.
set grid_cell_area [expr {$core_w * $core_h / ($grid_rows * $grid_cols)}]
set col_w [expr {$core_w / double($grid_cols)}]
set row_h [expr {$core_h / double($grid_rows)}]

# grid_occ(r,c) = sum of placed-cell areas in that bin (um^2)
for {set r 0} {$r < $grid_rows} {incr r} {
    for {set c 0} {$c < $grid_cols} {incr c} {
        set grid_occ($r,$c) 0.0
    }
}

foreach inst_name [array names cell_info] {
    lassign $cell_info($inst_name) cx cy area
    set c [expr {int(($cx - $core_x0) / $col_w)}]
    set r [expr {int(($cy - $core_y0) / $row_h)}]
    # clamp to grid bounds
    if {$c < 0} {set c 0}
    if {$c >= $grid_cols} {set c [expr {$grid_cols - 1}]}
    if {$r < 0} {set r 0}
    if {$r >= $grid_rows} {set r [expr {$grid_rows - 1}]}
    set grid_occ($r,$c) [expr {$grid_occ($r,$c) + $area}]
}

# ===========================================================================
# 4. Identify critical path cells and their locations
# ===========================================================================
# Get worst $nworst_paths paths by re-running report_checks
sta::redirect_string_begin
report_checks -path_delay max -format full_clock_expanded \
    -fields {slew capacitance input_pin net} -digits 4 \
    -endpoint_path_count 1 -group_path_count $nworst_paths \
    -sort_by_slack -slack_max 0.0
set worst_rpt [sta::redirect_string_end]

# Extract instance names from path lines:
# Lines like:  <delay>  <...>  inst_name/pin_name  (net_name)
# We target the "inst/pin" column which starts at col 0 after leading spaces
# The full_clock_expanded format has the instance column clearly identifiable.
# We stop at "data arrival time" to skip clock path cells.
set path_insts {}
set in_data_path 0
foreach line [split $worst_rpt "\n"] {
    if {[regexp {^Startpoint:} $line]} {
        set in_data_path 1
    }
    if {!$in_data_path} continue
    if {[regexp {data arrival time} $line]} {
        set in_data_path 0
        continue
    }
    # Match pin references: leading whitespace, optional delay, then inst/pin
    # Format: "  <float>  <float>  <float>  inst_name/pin"
    # Lines with pin references have direction char (^ or v) before inst/pin
    # Format: "  cap  slew  delay  time  ^ inst_name/pin_name  (master)"
    # Instance names may include $, _, digits, letters, brackets
    if {[regexp {[\^v]\s+([A-Za-z_\$\[][A-Za-z0-9_.\$\[\]]*)/[A-Za-z_][A-Za-z0-9_.]*} $line -> inst]} {
        # Skip clock buffers
        if {[regexp {^clkbuf_} $inst]} continue
        if {[lsearch -exact $path_insts $inst] < 0} {
            lappend path_insts $inst
        }
    }
}

# For each critical-path inst, look up location and compute local density
set crit_cells {}
set bbox_xmin  1e18
set bbox_ymin  1e18
set bbox_xmax -1e18
set bbox_ymax -1e18

foreach inst_name $path_insts {
    if {![info exists cell_info($inst_name)]} continue
    lassign $cell_info($inst_name) cx cy area

    # Bounding box update
    if {$cx < $bbox_xmin} {set bbox_xmin $cx}
    if {$cy < $bbox_ymin} {set bbox_ymin $cy}
    if {$cx > $bbox_xmax} {set bbox_xmax $cx}
    if {$cy > $bbox_ymax} {set bbox_ymax $cy}

    # Local density: sum area of all cells within density_radius
    set local_occ  0.0
    set win_area   [expr {(2.0 * $density_radius) * (2.0 * $density_radius)}]
    set x0 [expr {$cx - $density_radius}]
    set x1 [expr {$cx + $density_radius}]
    set y0 [expr {$cy - $density_radius}]
    set y1 [expr {$cy + $density_radius}]
    foreach nb [array names cell_info] {
        lassign $cell_info($nb) nx ny na
        if {$nx >= $x0 && $nx <= $x1 && $ny >= $y0 && $ny <= $y1} {
            set local_occ [expr {$local_occ + $na}]
        }
    }
    set local_density [expr {$local_occ / $win_area * 100.0}]

    # Grid bin
    set col [expr {int(($cx - $core_x0) / $col_w)}]
    set row [expr {int(($cy - $core_y0) / $row_h)}]
    if {$col < 0} {set col 0}
    if {$col >= $grid_cols} {set col [expr {$grid_cols - 1}]}
    if {$row < 0} {set row 0}
    if {$row >= $grid_rows} {set row [expr {$grid_rows - 1}]}

    lappend crit_cells [list $inst_name $cx $cy [format %.1f $local_density] $row $col]
}

if {$bbox_xmin > 1e17} {
    # No critical cells found with placement info; set bbox to full core
    set bbox_xmin $core_x0 ; set bbox_ymin $core_y0
    set bbox_xmax $core_x1 ; set bbox_ymax $core_y1
}
set bbox_w [expr {$bbox_xmax - $bbox_xmin}]
set bbox_h [expr {$bbox_ymax - $bbox_ymin}]
set bbox_area [expr {$bbox_w * $bbox_h}]
set core_area [expr {$core_w * $core_h}]
set bbox_pct  [expr {$core_area > 0 ? $bbox_area / $core_area * 100.0 : 0.0}]

# ===========================================================================
# 5. Free row slot estimation
# ===========================================================================
# Use report_design_area to get authoritative utilization, then derive free area.
# ASAP7 standard cell height = 2.16 um (7.5-track cell @ 0.288 um pitch × 7.5).
sta::redirect_string_begin
report_design_area
set area_rpt [sta::redirect_string_end]

set util_pct_da  0.0
set area_um2_da  0.0
if {[regexp {Design area ([0-9.eE+-]+) um\^2 ([0-9.eE+-]+)% utilization} $area_rpt -> av uv]} {
    set area_um2_da [expr {double($av)}]
    set util_pct_da [expr {double($uv)}]
}

# ASAP7 single-height row = 2.16 um
set row_height_um 2.16
set row_count     [expr {int(ceil($core_h / $row_height_um))}]

# Derive total placeable area from utilization and placed-cell area
# util% = placed_cell_area / total_placeable_area * 100
set total_row_area 0.0
if {$util_pct_da > 0} {
    set total_row_area [expr {$area_um2_da / ($util_pct_da / 100.0)}]
} else {
    set total_row_area [expr {$core_w * $core_h}]
}

set free_area_um2 [expr {$total_row_area - $area_um2_da}]
if {$free_area_um2 < 0} { set free_area_um2 0.0 }

# Express free area as number of x4-drive-strength standard cells.
# ASAP7 BUFx4 footprint ≈ 0.864 um wide × 2.16 um tall = ~1.87 um^2
set std_cell_area_est 1.87
set free_slots_est [expr {int($free_area_um2 / $std_cell_area_est)}]
set free_pct [expr {$total_row_area > 0 ? $free_area_um2 / $total_row_area * 100.0 : 0.0}]

# ===========================================================================
# 6. Run detailed_placement and capture its output
# ===========================================================================
sta::redirect_string_begin
check_placement -verbose
set placement_check_rpt [sta::redirect_string_end]

# ===========================================================================
# 7. Assemble and write the report
# ===========================================================================
set report_file [file join $output_dir ${stage_tag}_placement_report.txt]
set fh [open $report_file w]

puts $fh "================================================================="
puts $fh "PLACEMENT REPORT  stage=${stage_tag}"
puts $fh "================================================================="
puts $fh ""

# --- Die / Core ---
puts $fh "--- Die & Core Dimensions ---"
puts $fh [format "Die  : %.3f x %.3f um  (LL: %.3f,%.3f  UR: %.3f,%.3f)" \
    $die_w $die_h $die_x0 $die_y0 $die_x1 $die_y1]
puts $fh [format "Core : %.3f x %.3f um  (LL: %.3f,%.3f  UR: %.3f,%.3f)" \
    $core_w $core_h $core_x0 $core_y0 $core_x1 $core_y1]
puts $fh [format "Rows : %d  (row height: %.3f um)" $row_count $row_height_um]
puts $fh ""

# --- Area summary ---
puts $fh "--- Cell Area Summary ---"
puts $fh [format "Total placed cells      : %d" $total_placed]
puts $fh [format "Total placed cell area  : %.2f um^2" $total_cell_area]
puts $fh [format "Total row (site) area   : %.2f um^2" $total_row_area]
puts $fh [format "Free site area          : %.2f um^2  (%.1f%% free)" \
    $free_area_um2 $free_pct]
puts $fh [format "Free slots (est. x4 BUF): ~%d cells" $free_slots_est]
puts $fh ""

# --- Spatial utilization grid ---
puts $fh [format "--- Spatial Utilization Grid (%dx%d)  (row 0=bottom, col 0=left) ---" \
    $grid_rows $grid_cols]
puts $fh [format "Each bin is %.2f x %.2f um  (bin area: %.2f um^2)" \
    $col_w $row_h [expr {$col_w * $row_h}]]
puts $fh ""

# Header row
set hdr "     |"
for {set c 0} {$c < $grid_cols} {incr c} {
    append hdr [format " col%-2d|" $c]
}
puts $fh $hdr
puts $fh [string repeat "-" [string length $hdr]]

# Print from top row down (row grid_rows-1 first for natural orientation)
for {set r [expr {$grid_rows - 1}]} {$r >= 0} {incr r -1} {
    set line [format "row%-2d|" $r]
    for {set c 0} {$c < $grid_cols} {incr c} {
        set occ  $grid_occ($r,$c)
        set bin_area [expr {$col_w * $row_h}]
        set pct [expr {$bin_area > 0 ? $occ / $bin_area * 100.0 : 0.0}]
        append line [format " %4.0f%%|" $pct]
    }
    puts $fh $line
}
puts $fh ""

# --- Critical path cell locations ---
puts $fh [format "--- Critical Path Cells (top %d worst paths) ---" $nworst_paths]
if {[llength $crit_cells] == 0} {
    puts $fh "  (no critical path cells with placement information found)"
} else {
    puts $fh [format "  %-40s  %8s  %8s  %8s  %6s  %6s" \
        "Instance" "X(um)" "Y(um)" "LocalDens%" "Row" "Col"]
    puts $fh "  [string repeat - 85]"
    foreach entry $crit_cells {
        lassign $entry iname cx cy ld gr gc
        puts $fh [format "  %-40s  %8.3f  %8.3f  %8s%%  %6d  %6d" \
            $iname $cx $cy $ld $gr $gc]
    }
}
puts $fh ""

# --- Worst-path bounding box ---
puts $fh "--- Critical Path Bounding Box ---"
puts $fh [format "  BBox LL : (%.3f, %.3f) um" $bbox_xmin $bbox_ymin]
puts $fh [format "  BBox UR : (%.3f, %.3f) um" $bbox_xmax $bbox_ymax]
puts $fh [format "  BBox W x H : %.3f x %.3f um" $bbox_w $bbox_h]
puts $fh [format "  BBox area  : %.2f um^2  (%.1f%% of core)" $bbox_area $bbox_pct]
puts $fh ""

# --- Placement check output ---
puts $fh "--- Placement Check (check_placement -verbose) ---"
if {[string trim $placement_check_rpt] eq ""} {
    puts $fh "  (no output — placement is clean)"
} else {
    puts $fh $placement_check_rpt
}
puts $fh ""
puts $fh "================================================================="

close $fh

puts "Placement report written: $report_file"
