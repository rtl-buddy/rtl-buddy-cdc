create_clock -name clk -period 10.0 [get_ports clk]
# The fix CDC-011 asks for: type the synchronous reset (and the data
# control) to the clock that samples them.
set_input_delay -clock clk 1.0 [get_ports srst]
set_input_delay -clock clk 1.0 [get_ports dctl]
