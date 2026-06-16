create_clock -name clkA -period 10.0 [get_ports clkA]
create_clock -name clkB -period 7.0  [get_ports clkB]
create_generated_clock -name gdiv -master_clock clkA \
    -source [get_ports clkA] -divide_by 2 [get_pins gdiv]
set_clock_groups -asynchronous -group {clkA gdiv} -group {clkB}
