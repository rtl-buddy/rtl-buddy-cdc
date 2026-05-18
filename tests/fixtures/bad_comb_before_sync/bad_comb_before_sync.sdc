create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# `a` and `b` originate in src_clk's domain — declare to keep CDC-011
# silent (this fixture targets CDC-003 / comb-before-sync, not the
# unconstrained-input shape).
set_input_delay -clock src_clk 1.0 [get_ports a]
set_input_delay -clock src_clk 1.0 [get_ports b]
