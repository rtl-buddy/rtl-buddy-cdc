create_clock -name cfg_clk -period 20.0 [get_ports cfg_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {cfg_clk} -group {dst_clk}
set_input_delay -clock cfg_clk 1.0 [get_ports {cfg_we cfg_mode_in cfg_data_in}]
