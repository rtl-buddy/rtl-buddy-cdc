create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
# Intentionally NO `set_input_delay` on `in[7:0]` — that's the gap
# CDC-011 (issue #97) surfaces.
