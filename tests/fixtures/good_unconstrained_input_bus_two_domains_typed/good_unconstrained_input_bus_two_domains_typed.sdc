create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
# `in[7:0]` arrives synchronous to clk_a. CDC-011 silent (typed),
# CDC-004 silent (bus crossing into clk_b uses a registered pre-
# stage in the same source domain — the typed clk_a port is treated
# as the source register from the rule's perspective).
set_input_delay -clock clk_a 1.0 [get_ports in]
