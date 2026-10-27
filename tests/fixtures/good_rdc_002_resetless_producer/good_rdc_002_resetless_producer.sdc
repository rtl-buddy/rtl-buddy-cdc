create_clock -name clk -period 5.0 [get_ports clk]
set_input_delay -clock clk 1.0 [get_ports rst_i]
set_input_delay -clock clk 1.0 [get_ports d]
