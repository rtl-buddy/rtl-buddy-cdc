create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period  7.5 [get_ports clk_b]
create_clock -name clk_c -period  5.0 [get_ports clk_c]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b} -group {clk_c}
# Intentionally NO `set_input_delay` on `in` — CDC-011's purpose is
# to flag the missing constraint. Adding typing would invalidate the
# fixture.
