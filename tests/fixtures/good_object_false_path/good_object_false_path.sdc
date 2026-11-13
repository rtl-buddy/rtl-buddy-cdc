create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports {we wdata}]
# Quasi-static configuration: written once while dst_clk logic is idle.
# Synthesis-style register names; a clock endpoint on the other side.
set_false_path -from [get_cells u_cfg/mode_q_reg*] -to [get_clocks dst_clk]
set_false_path -from [get_pins u_cfg/enable_q_reg/Q] -to [get_cells en_dst_reg]
