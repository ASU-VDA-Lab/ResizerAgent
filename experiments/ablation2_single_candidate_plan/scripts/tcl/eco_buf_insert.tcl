# eco_buf_insert.tcl — ECO buffer insertion proc
#
# eco_insert_buffer: insert a buffer on a net, moving selected sinks to the new
# buffer output. Works via ODB API (make_net / make_instance / connect_pin) —
# does NOT use OpenROAD's built-in insert_buffer command (which crashes after
# replace_cell per bug EST-0104).
#
# Usage:
#   eco_insert_buffer <net_name> <driver_pin> <buf_cell> <buf_name> \
#       [-sinks all|{inst/pin ...}] \
#       [-at driver|centroid|{x y}] \
#       [-in_pin <pin>] [-out_pin <pin>]
#
# Arguments:
#   net_name    — name of the net being split (informational only)
#   driver_pin  — "inst/pin" of the driving output pin on the original net
#   buf_cell    — cell master to instantiate (e.g. BUFx2_ASAP7_75t_R)
#   buf_name    — instance name for the new buffer
#   -sinks      — which sink iterms to move: "all" (default) or a list of
#                 "inst/pin" pairs
#   -at         — placement: "driver" (near driver), "centroid" (avg of sinks),
#                 or "{x y}" explicit micron coords
#   -in_pin     — input pin name on the buffer cell (default: A)
#   -out_pin    — output pin name on the buffer cell (default: Y)

proc _eco_pin_to_dbinst { sta_pin } {
    set sta_inst [[lindex $sta_pin 0] instance]
    set dbinst [sta::sta_to_db_inst $sta_inst]
    if {$dbinst eq "NULL" || $dbinst eq ""} { error "_eco_pin_to_dbinst: NULL" }
    return $dbinst
}

proc _eco_pin_to_location_um { sta_pin } {
    set dbinst [_eco_pin_to_dbinst $sta_pin]
    set loc [$dbinst getLocation]
    return [list [ord::dbu_to_microns [lindex $loc 0]] \
                 [ord::dbu_to_microns [lindex $loc 1]]]
}

proc _eco_split_sink_key { sp } {
    set t [string trim $sp "{} "]
    set slash [string last "/" $t]
    if {$slash < 0} { return {} }
    return [list [string range $t 0 [expr {$slash-1}]] \
                 [string range $t [expr {$slash+1}] end]]
}

proc eco_insert_buffer { net_name driver_pin buf_cell buf_name args } {
    set sink_pins   "all"
    set at_mode     "centroid"
    set at_xy       ""
    set buf_in_pin  "A"
    set buf_out_pin "Y"

    for {set i 0} {$i < [llength $args]} {incr i} {
        set k [lindex $args $i]
        set v [lindex $args [expr {$i+1}]]
        switch -- $k {
            "-sinks"   { set sink_pins $v; incr i }
            "-at"      {
                if {$v eq "driver" || $v eq "centroid"} {
                    set at_mode $v
                } else {
                    set at_mode "explicit"; set at_xy $v
                }
                incr i }
            "-in_pin"  { set buf_in_pin  $v; incr i }
            "-out_pin" { set buf_out_pin $v; incr i }
            default    { error "eco_insert_buffer: unknown option '$k'" }
        }
    }

    set block [ord::get_db_block]

    set drv_pin [get_pins $driver_pin]
    if {$drv_pin eq "" || $drv_pin eq "NULL"} {
        error "eco_insert_buffer: driver pin '$driver_pin' not found"
    }
    set drv_dbinst [_eco_pin_to_dbinst $drv_pin]
    set drv_odb_name [$drv_dbinst getName]
    lassign [_eco_pin_to_location_um $drv_pin] drv_x drv_y

    set drv_net_sta [[lindex $drv_pin 0] net]
    set db_net [sta::sta_to_db_net $drv_net_sta]
    if {$db_net eq "NULL" || $db_net eq ""} {
        error "eco_insert_buffer: cannot get ODB net from driver '$driver_pin'"
    }

    set move_all [expr {$sink_pins eq "all"}]
    set requested {}
    if {!$move_all} {
        foreach sp $sink_pins {
            set kp [_eco_split_sink_key $sp]
            if {$kp eq ""} { puts "# WARN bad sink '$sp'"; continue }
            dict set requested "[lindex $kp 0]/[lindex $kp 1]" 1
        }
    }

    set sink_iterms {}
    foreach iterm [$db_net getITerms] {
        set inst_name [[$iterm getInst] getName]
        set pin_name  [[$iterm getMTerm] getName]
        set io        [[$iterm getMTerm] getIoType]
        if {$inst_name eq $drv_odb_name && ($io eq "OUTPUT" || $io eq "INOUT")} { continue }
        if {$move_all} {
            lappend sink_iterms $iterm
        } elseif {[dict exists $requested "${inst_name}/${pin_name}"]} {
            lappend sink_iterms $iterm
        }
    }

    set sink_bterms {}
    if {$move_all} { foreach bt [$db_net getBTerms] { lappend sink_bterms $bt } }

    set total [expr {[llength $sink_iterms] + [llength $sink_bterms]}]
    if {$total == 0} { error "eco_insert_buffer: no sinks found on '$net_name'" }

    # Determine placement location
    set loc_x $drv_x; set loc_y $drv_y
    switch -- $at_mode {
        "driver"   { set loc_x [expr {$drv_x + 0.2}] }
        "explicit" { lassign $at_xy loc_x loc_y }
        "centroid" {
            set sx 0.0; set sy 0.0; set n 0
            foreach it $sink_iterms {
                set L [[$it getInst] getLocation]
                set sx [expr {$sx + [ord::dbu_to_microns [lindex $L 0]]}]
                set sy [expr {$sy + [ord::dbu_to_microns [lindex $L 1]]}]
                incr n
            }
            if {$n > 0} { set loc_x [expr {$sx/$n}]; set loc_y [expr {$sy/$n}] }
        }
    }

    # Create new net and buffer instance
    set new_net_name "${buf_name}_net"
    set new_net [make_net $new_net_name]
    make_instance $buf_name $buf_cell

    set old_net [get_nets [get_name $drv_net_sta]]
    connect_pin $old_net [get_pins "${buf_name}/${buf_in_pin}"]
    connect_pin $new_net [get_pins "${buf_name}/${buf_out_pin}"]

    # Move sinks to new net
    set moved 0
    foreach it $sink_iterms {
        set full "[[$it getInst] getName]/[[$it getMTerm] getName]"
        if {[catch {
            set p [get_pins $full]
            disconnect_pin $old_net $p
            connect_pin    $new_net $p
            incr moved
        } err]} { puts "# WARN rewire $full: $err" }
    }
    foreach bt $sink_bterms {
        set name [$bt getName]
        if {[catch {
            set p [get_ports $name]
            disconnect_pin $old_net $p
            connect_pin    $new_net $p
            incr moved
        } err]} { puts "# WARN rewire port $name: $err" }
    }

    place_inst -name $buf_name -cell $buf_cell \
        -location [list $loc_x $loc_y] -status PLACED

    puts "# eco_insert_buffer OK: $buf_name ($buf_cell) at ($loc_x,$loc_y) moved $moved/$total sinks"
    return $buf_name
}
