create_clock -name clk -period 10 [get_ports clk]
set_input_delay 1 -clock clk [get_ports d_in]
