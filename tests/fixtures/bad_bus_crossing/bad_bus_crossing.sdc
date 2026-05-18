create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
# `src_data` is sourced from src_clk's domain — declare it so CDC-011
# doesn't pick it up as unconstrained (this fixture exercises CDC-004's
# bus-crossing detection, not the unconstrained-input shape).
set_input_delay -clock src_clk 1.0 [get_ports src_data]
