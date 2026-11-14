create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period  7.5 [get_ports clk_b]
create_clock -name clk_c -period  4.0 [get_ports clk_c]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b} -group {clk_c}
set_input_delay -clock clk_a 1.0 [get_ports d_a]
set_input_delay -clock clk_b 1.0 [get_ports d_b]
# Mode bit for the clock mux. Braced so Tcl does not substitute [1].
set_case_analysis 1 [get_ports {mode[1]}]
