create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 13.0 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay  -clock clk_a 1.0 [get_ports {d_a bus_a}]
set_input_delay  -clock clk_b 1.0 [get_ports {d_b en_b}]
set_output_delay -clock clk_a 1.0 [get_ports {q_a q_bus q_pair_a}]
set_output_delay -clock clk_b 1.0 [get_ports {q_b q_pair_b}]
