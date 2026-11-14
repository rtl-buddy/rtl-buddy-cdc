# dpretet/async_fifo (async_fifo top). Written for rtl-buddy-cdc's
# integration corpus; the upstream repository ships no SDC.
#
# Write side on wclk, read side on rclk, unrelated sources. The
# resets are asynchronous inputs and stay untyped (async reset pins
# are out of scope for port typing; the RDC rules own them).
create_clock -name wclk -period 10.0 [get_ports wclk]
create_clock -name rclk -period 12.5 [get_ports rclk]
set_clock_groups -asynchronous -group {wclk} -group {rclk}

set_input_delay  -clock wclk 1.0 [get_ports {winc wdata}]
set_output_delay -clock wclk 1.0 [get_ports {wfull awfull}]
set_input_delay  -clock rclk 1.0 [get_ports {rinc}]
set_output_delay -clock rclk 1.0 [get_ports {rdata rempty arempty}]
