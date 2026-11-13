# Port clk_a carries two clocks. clk_a2 is added with -add, so clk_a is
# not replaced: both exist on the port (rtl-buddy-cdc#330).
create_clock -name clk_a  -period 10.0 [get_ports clk_a]
create_clock -name clk_a2 -period 40.0 -add [get_ports clk_a]
create_clock -name clk_b  -period 10.0 [get_ports clk_b]
# The two modes on clk_a never coexist.
set_clock_groups -physically_exclusive -group {clk_a} -group {clk_a2}
# clk_a is synchronous to clk_b (undeclared pair); clk_a2 is not.
set_clock_groups -asynchronous -group {clk_a2} -group {clk_b}
set_input_delay -clock clk_a 1.0 [get_ports d_a]
set_input_delay -clock clk_b 1.0 [get_ports d_b]
