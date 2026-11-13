create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period  7.5 [get_ports clk_b]
create_clock -name clk_c -period  5.0 [get_ports clk_c]
# Two statements. clk_a and clk_c are never separated by one, so they
# stay related (timed together).
set_clock_groups -asynchronous -name grp_ab -group {clk_a} -group {clk_b}
set_clock_groups -asynchronous -name grp_bc -group [get_clocks clk_b] \
    -group [get_clocks clk_c]
set_input_delay -clock clk_a 1.0 [get_ports d_a]
set_input_delay -clock clk_b 1.0 [get_ports d_b]
