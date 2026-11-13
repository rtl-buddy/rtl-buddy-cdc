create_clock -name clk_a  -period 10.0 [get_ports clk_a]
create_clock -name clk_b  -period 13.0 [get_ports clk_b]
# vclk_a: the external receiver's clock, synchronous to clk_a (it is
# never placed in an asynchronous group with it).
create_clock -name vclk_a -period 10.0
# clk_a_fwd: clk_a forwarded out of the design on clk_fwd.
create_generated_clock -name clk_a_fwd -master_clock clk_a \
    -source [get_ports clk_a] -divide_by 1 [get_ports clk_fwd]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay  -clock clk_a 1.0 [get_ports {d0 d1 d_bus}]
set_input_delay  -clock clk_b 1.0 [get_ports d_b]
set_output_delay -clock clk_a      1.0 [get_ports q_reg]
set_output_delay -clock vclk_a     1.0 [get_ports q_comb]
set_output_delay -clock clk_a_fwd  1.0 [get_ports q_bus]
set_output_delay -clock clk_b      1.0 [get_ports q_b]
