create_clock -name wr_clk -period 10.0 [get_ports wr_clk]
create_clock -name rd_clk -period 7.5  [get_ports rd_clk]
set_clock_groups -asynchronous -group {wr_clk} -group {rd_clk}
set_input_delay -clock wr_clk 1.0 [get_ports {wr_en wr_addr wr_data}]
set_input_delay -clock rd_clk 1.0 [get_ports {rd_addr}]
