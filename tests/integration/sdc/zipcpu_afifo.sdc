# ZipCPU/wb2axip afifo (asynchronous FIFO), default parameters.
# Written for rtl-buddy-cdc's integration corpus.
#
# i_wclk / i_rclk are unrelated sources. The active-low resets are
# asynchronous inputs and stay untyped.
create_clock -name i_wclk -period 10.0 [get_ports i_wclk]
create_clock -name i_rclk -period 12.5 [get_ports i_rclk]
set_clock_groups -asynchronous -group {i_wclk} -group {i_rclk}

set_input_delay  -clock i_wclk 1.0 [get_ports {i_wr i_wr_data}]
set_output_delay -clock i_wclk 1.0 [get_ports {o_wr_full}]
set_input_delay  -clock i_rclk 1.0 [get_ports {i_rd}]
set_output_delay -clock i_rclk 1.0 [get_ports {o_rd_data o_rd_empty}]
