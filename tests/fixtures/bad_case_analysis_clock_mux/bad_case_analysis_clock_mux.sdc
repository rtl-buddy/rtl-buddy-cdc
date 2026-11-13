create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period  7.5 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1.0 [get_ports d_in]
set_input_delay -clock clk_a 1.0 [get_ports sel]
# The constrained mode selects clk_b on the clock mux (#325).
set_case_analysis 1 [get_ports sel]
