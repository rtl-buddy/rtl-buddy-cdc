create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
# `in` arrives synchronous to clk_a — the SDC author has answered
# CDC-011's question. The clk_b-side capture goes through a 2FF
# synchronizer in the RTL, so CDC-001 stays silent too.
set_input_delay -clock clk_a 1.0 [get_ports in]
