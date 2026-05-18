create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
# `a` and `b` are async logic-level signals from a foreign domain —
# model that with a virtual clock + async-group + set_input_delay
# typing, so CDC-006 still fires on the glitchy-comb-source shape
# (its real target) while CDC-011 stays silent on the port (the SDC
# now answers "what domain does this port belong to?").
create_clock -name vclk_ext -period 10.0
set_clock_groups -asynchronous -group {vclk_ext} -group {dst_clk}
set_input_delay -clock vclk_ext 1.0 [get_ports a]
set_input_delay -clock vclk_ext 1.0 [get_ports b]
