# Only one declared clock; `a` and `b` are unclocked top-level inputs
# (asynchronous logic-level signals from a foreign domain).
create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
