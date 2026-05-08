create_clock -name dst_clk     -period 7.5  [get_ports dst_clk]
create_clock -name foreign_clk -period 12.0
set_clock_groups -asynchronous -group {dst_clk} -group {foreign_clk}
set_input_delay -clock foreign_clk 1.0 [get_ports d_in]
