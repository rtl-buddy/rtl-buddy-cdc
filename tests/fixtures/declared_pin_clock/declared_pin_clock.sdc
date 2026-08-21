create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
# The divided internal net, declared directly on its pin with a plain
# create_clock (issue #270) — not a create_generated_clock, so it lands
# in Clock.ports / clock_for_port rather than ClockSpec.pin_clocks.
create_clock -name div_clk -period 20.0 [get_pins clk_div]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1.0 [get_ports din]
