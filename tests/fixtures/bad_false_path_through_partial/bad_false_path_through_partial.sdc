create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period  7.5 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports {we wdata stat_in}]
# Covers the configuration bus only: stat_q does not pass u_bridge.
set_false_path -through [get_pins u_bridge/cfg_o]
