create_clock -name tclk -period 10.0 [get_ports tclk]
create_clock -name sclk -period  7.5 [get_ports sclk]
set_clock_groups -asynchronous -group {tclk} -group {sclk}
# `d` is typed against the same clock that captures it.
set_input_delay -clock sclk 1.0 [get_ports d]
