create_clock -name clk -period 10.0 [get_ports clk]
create_generated_clock -name clk_div2 -master_clock clk \
    -source [get_ports clk] -divide_by 2 [get_pins clk_div2]
set_input_delay -clock clk 1.0 [get_ports d_in]
# clk and clk_div2 are NOT in any async group: they share a master and
# must be treated as the same domain for CDC.
