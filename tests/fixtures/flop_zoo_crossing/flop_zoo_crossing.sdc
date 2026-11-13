create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.0  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# Type every data and control input so CDC-011 stays silent: the
# fixture is about the flop families, not unconstrained inputs.
set_input_delay -clock src_clk 1.0 [get_ports d_in]
set_input_delay -clock dst_clk 1.0 [get_ports {rst_n set_n aload ad en srst}]
