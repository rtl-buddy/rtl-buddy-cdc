create_clock -name clkA -period 10.0 [get_ports clkA]
create_clock -name clkB -period 7.0  [get_ports clkB]
set_clock_groups -asynchronous -group {clkA} -group {clkB}
