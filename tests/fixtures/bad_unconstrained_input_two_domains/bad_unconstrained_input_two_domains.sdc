create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
# Intentionally NO `set_input_delay` on `in` — that's the SDC gap that
# CDC-011 (issue #97) is meant to surface. Adding typing here would
# invalidate the fixture for its purpose.
