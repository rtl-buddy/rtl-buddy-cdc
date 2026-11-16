create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
# cfg_in[2:0] on clk_b; cfg_in[3] is removed, so it stays untyped.
set_input_delay -clock clk_b 1.0 [remove_from_collection [get_ports {cfg_in[*]}] [get_ports {cfg_in[3]}]]
# mix_in is typed bit by bit to two clocks.
set_input_delay -clock clk_a 1.0 [get_ports {mix_in[0] mix_in[1]}]
set_input_delay -clock clk_b 1.0 [get_ports {mix_in[2] mix_in[3]}]
