# clk_a2x is a PLL output (x2 of clk_a) brought in on its own port.
create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_generated_clock -name clk_a2x -source [get_ports clk_a] \
    -master_clock clk_a -multiply_by 2 [get_ports clk_a2x]
create_clock -name clk_b -period 7.0 [get_ports clk_b]
create_clock -name clk_c -period 3.3 [get_ports clk_c]
# Several clocks in one group: clk_a and clk_a2x are timed together.
set_clock_groups -asynchronous \
    -group {clk_a clk_a2x} -group {clk_b} -group {clk_c}
set_input_delay -clock clk_a   1.0 [get_ports d_a]
set_input_delay -clock clk_a2x 0.5 [get_ports d_x]
set_input_delay -clock clk_b   1.0 [get_ports d_b]
