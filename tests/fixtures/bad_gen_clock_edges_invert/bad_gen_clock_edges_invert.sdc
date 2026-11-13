create_clock -name clk   -period 10.0 [get_ports clk]
create_clock -name clk_x -period  6.0 [get_ports clk_x]
# Divide-by-3, high for one clk cycle: rising on edge 1, falling on
# edge 3, rising again on edge 7.
create_generated_clock -name ck_div3 -master_clock clk \
    -source [get_ports clk] -edges {1 3 7} [get_pins div3_q]
create_generated_clock -name ck_inv -master_clock clk \
    -source [get_ports clk] -invert -divide_by 1 [get_pins clk_n]
create_generated_clock -name ck_gated -master_clock clk \
    -source [get_ports clk] -combinational [get_pins clk_g]
# Roots only: the generated clocks follow clk.
set_clock_groups -asynchronous -group {clk} -group {clk_x}
set_input_delay -clock clk   1.0 [get_ports {d_a en_in}]
set_input_delay -clock clk_x 1.0 [get_ports d_x]
