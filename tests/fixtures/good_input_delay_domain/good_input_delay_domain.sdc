create_clock -name dst_clk -period 7.5 [get_ports dst_clk]
# Tell CDC that `a` and `b` are already synchronous to dst_clk —
# typically the case when an upstream block on the same chip-level
# clock drives these inputs. CDC-006 should NOT fire.
set_input_delay -clock dst_clk 1.0 [get_ports a]
set_input_delay -clock dst_clk 1.0 [get_ports b]
