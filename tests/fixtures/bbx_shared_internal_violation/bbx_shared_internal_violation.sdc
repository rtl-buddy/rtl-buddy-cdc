create_clock -name clk_s -period 10.0 [get_ports clk_s]
create_clock -name clk_c -period 7.0 [get_ports clk_c]
set_clock_groups -asynchronous -group {clk_s} -group {clk_c}
set_input_delay -clock clk_s 1.0 [get_ports d0]
set_input_delay -clock clk_s 1.0 [get_ports d1]
