create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period  7.5 [get_ports clk_b]
create_clock -name clk_c -period  5.0 [get_ports clk_c]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b} -group {clk_c}
# `in` arrives synchronous to clk_a — the SDC author asserts a single
# domain rather than leaving the analyzer to default through the
# AND-of-clocks. clk_a specifically because that's the root the
# destination flop resolves to (the trace walks the outer `$and`'s
# inputs and the "A" arm's resolution wins via `a_root or b_root`).
# CDC-011 stays silent (port is typed) and CDC-001 stays silent
# (source/destination domains match after resolution).
set_input_delay -clock clk_a 1.0 [get_ports in]
