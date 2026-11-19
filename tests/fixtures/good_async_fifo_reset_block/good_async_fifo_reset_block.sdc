create_clock -name wclk -period 10.0 [get_ports wclk]
create_clock -name rclk -period 7.0  [get_ports rclk]
set_clock_groups -asynchronous -group {wclk} -group {rclk}
set_input_delay  -clock wclk 1.0 [get_ports {wr_en wr_data}]
set_input_delay  -clock rclk 1.0 [get_ports rd_en]
set_output_delay -clock wclk 1.0 [get_ports wr_full]
set_output_delay -clock rclk 1.0 [get_ports {op arg rd_empty}]
# The storage words are not reset, so while wrst_n is low they hold: the
# reset is sampled synchronously by them (CDC-011 without this typing).
set_input_delay  -clock wclk 0.0 [get_ports wrst_n]
