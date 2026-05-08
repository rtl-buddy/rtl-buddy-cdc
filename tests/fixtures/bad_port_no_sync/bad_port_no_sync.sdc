create_clock -name dst_clk     -period 7.5  [get_ports dst_clk]
create_clock -name foreign_clk -period 12.0
set_clock_groups -asynchronous -group {dst_clk} -group {foreign_clk}
# d_in is sourced from foreign_clk's domain — declared so the analyzer
# can promote the port→flop path to a first-class crossing.
set_input_delay -clock foreign_clk 1.0 [get_ports d_in]
