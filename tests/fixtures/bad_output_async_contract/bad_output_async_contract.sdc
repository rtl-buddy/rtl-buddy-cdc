create_clock -name clk_a  -period 10.0 [get_ports clk_a]
create_clock -name clk_b  -period 13.0 [get_ports clk_b]
# vclk_b is a virtual clock: the external receiver's capture clock, with
# no port on this design. Typical for an output contract.
create_clock -name vclk_b -period 13.0
set_clock_groups -asynchronous -group {clk_a} -group {clk_b vclk_b}
set_input_delay  -clock clk_a 1.0 [get_ports {d0 d1 d_bus}]
set_input_delay  -clock clk_b 1.0 [get_ports d_b]
set_output_delay -clock vclk_b 1.0 [get_ports q_reg]
set_output_delay -clock vclk_b 1.0 [get_ports q_comb]
set_output_delay -clock clk_b  1.0 [get_ports q_bus]
set_output_delay -clock clk_b  1.0 [get_ports q_mix]
