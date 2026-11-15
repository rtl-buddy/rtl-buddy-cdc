create_clock -name src_clk -period 4.0  [get_ports src_clk]
create_clock -name dst_clk -period 10.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 0.5 [get_ports {src_rst_n src_aload src_ad src_req src_a src_b src_c}]
set_input_delay -clock dst_clk 0.5 [get_ports {rst_n set_n aload ad srst}]
