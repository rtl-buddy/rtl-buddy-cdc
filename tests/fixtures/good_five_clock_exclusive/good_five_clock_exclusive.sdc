# Five clocks. All mutually asynchronous...
create_clock -name clk_sys -period 10.0 [get_ports clk_sys]
create_clock -name clk_m0  -period  8.0 [get_ports clk_m0]
create_clock -name clk_m1  -period  5.0 [get_ports clk_m1]
create_clock -name clk_io  -period 12.0 [get_ports clk_io]
create_clock -name clk_dbg -period 20.0 [get_ports clk_dbg]
set_clock_groups -asynchronous -group {clk_sys} -group {clk_m0} \
    -group {clk_m1} -group {clk_io} -group {clk_dbg}
# ...and two exclusive pairs: only one mode clock runs at a time, and
# the debug clock replaces the functional I/O clock in debug mode.
set_clock_groups -physically_exclusive -group {clk_m0} -group {clk_m1}
set_clock_groups -logically_exclusive  -group {clk_io} -group {clk_dbg}
set_input_delay -clock clk_m0  1.0 [get_ports d_m0]
set_input_delay -clock clk_m1  1.0 [get_ports d_m1]
set_input_delay -clock clk_io  1.0 [get_ports d_io]
set_input_delay -clock clk_dbg 1.0 [get_ports d_dbg]
