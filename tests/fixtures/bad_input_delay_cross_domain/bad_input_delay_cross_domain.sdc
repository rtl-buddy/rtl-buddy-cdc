create_clock -name dst_clk    -period 7.5  [get_ports dst_clk]
# foreign_clk is a virtual clock — declared so we can name a source
# domain for the inputs without needing a physical port for it. (Real
# projects do this for off-chip-sourced signals or when the upstream
# block's clock isn't visible at this level of hierarchy.)
create_clock -name foreign_clk -period 12.0
set_clock_groups -asynchronous -group {dst_clk} -group {foreign_clk}
set_input_delay -clock foreign_clk 1.0 [get_ports a]
set_input_delay -clock foreign_clk 1.0 [get_ports b]
