create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period  7.5 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# `ext_rst_assert` is sampled by a flop in src_clk's domain — declare
# to keep CDC-011 silent (this fixture targets CDC-007's reset
# distribution tree, not the unconstrained-input shape). `src_rst_n`
# and `dst_rst_n` are async reset ports driving ARST pins only, so
# they never reach a flop's D and CDC-011 stays silent on them.
set_input_delay -clock src_clk 1.0 [get_ports ext_rst_assert]
