create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.0 [get_ports clk_b]
create_clock -name clk_c -period 3.0 [get_ports clk_c]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b} -group {clk_c}
set_input_delay -clock clk_a 1.0 [get_ports flag_in]
