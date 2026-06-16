create_clock -name clk_x -period 10.0 [get_ports clk_x]
create_clock -name clk_d -period 7.0 [get_ports clk_d]
set_clock_groups -asynchronous -group {clk_x} -group {clk_d}
set_input_delay -clock clk_x 1.0 [get_ports din]
