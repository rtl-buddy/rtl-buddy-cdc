create_clock -name func_clk  -period 10.0 [get_ports func_clk]
create_clock -name scan_clk  -period 40.0 [get_ports scan_clk]
create_clock -name other_clk -period 13.3 [get_ports other_clk]
set_clock_groups -asynchronous -group {func_clk} -group {scan_clk} -group {other_clk}
# Type the data / control ports so the unconstrained-input rules stay
# silent — the behaviour under test is the scan-mode suppression.
set_input_delay -clock scan_clk  0.0 [get_ports d_in]
set_input_delay -clock other_clk 0.0 [get_ports e_in]
set_input_delay -clock func_clk  0.0 [get_ports scan_en]
set_input_delay -clock func_clk  0.0 [get_ports rst_n]
