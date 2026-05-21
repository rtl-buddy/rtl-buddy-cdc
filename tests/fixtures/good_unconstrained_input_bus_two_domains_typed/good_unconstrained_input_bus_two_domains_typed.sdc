create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
# `in[7:0]` and `load` arrive synchronous to clk_a. CDC-011 is silent
# because the ports are typed; CDC-004 is silent because the bus is
# sampled only under the dst-domain synchronized load enable.
set_input_delay -clock clk_a 1.0 [get_ports in]
set_input_delay -clock clk_a 1.0 [get_ports load]
