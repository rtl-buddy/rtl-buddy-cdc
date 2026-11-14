# Constraints generated from a clock table (needs the tcl backend).

set clock_table {
    clk_a 10.0
    clk_b  7.5
    clk_c  4.0
}

# One create_clock and one -group per clock.
set groups {}
foreach {name period} $clock_table {
    create_clock -name $name -period $period [get_ports $name]
    lappend groups -group [list $name]
}
set_clock_groups -asynchronous {*}$groups

# Data inputs, typed at 10% of their clock's period.
foreach {port clk} {d_a clk_a d_b clk_b} {
    set period [dict get $clock_table $clk]
    set_input_delay -clock $clk [expr {0.1 * $period}] [get_ports $port]
}
