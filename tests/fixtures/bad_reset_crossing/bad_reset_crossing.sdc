create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# `kill_req`, `d_in` originate in src_clk's domain — declare to keep
# CDC-011 silent (this fixture targets CDC-007 / async reset crossing,
# not the unconstrained-input shape). `global_rst_n` is an async reset
# port driving ARST pins only; it never reaches a flop's D, so CDC-011
# is already silent on it.
set_input_delay -clock src_clk 1.0 [get_ports kill_req]
# `d_in` is sampled by a flop on dst_clk (see the SV); type it
# accordingly so the port→flop walk doesn't flag a CDC-001 — the rule
# the fixture targets is CDC-007 on the reset, not the data path.
set_input_delay -clock dst_clk 1.0 [get_ports d_in]
