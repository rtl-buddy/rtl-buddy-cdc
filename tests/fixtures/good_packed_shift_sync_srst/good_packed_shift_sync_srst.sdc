create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# The data inputs originate in src_clk's domain and the two synchronous
# resets in dst_clk's — declare them so CDC-011 stays silent; this
# fixture is the packed sync-reset synchronizer, not the
# unconstrained-input shape.
set_input_delay -clock src_clk 1.0 [get_ports d_hi]
set_input_delay -clock src_clk 1.0 [get_ports d_lo]
set_input_delay -clock dst_clk 1.0 [get_ports rst]
set_input_delay -clock dst_clk 1.0 [get_ports rst_n]
