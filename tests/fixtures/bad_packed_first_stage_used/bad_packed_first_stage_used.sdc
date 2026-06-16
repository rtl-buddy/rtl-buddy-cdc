create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# `d_in` originates in src_clk's domain — declare it so CDC-011 stays
# silent; the crossing under test is src_q -> sync_sr.
set_input_delay -clock src_clk 1.0 [get_ports d_in]
