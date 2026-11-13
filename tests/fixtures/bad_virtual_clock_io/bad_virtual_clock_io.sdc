create_clock -name clk      -period 10.0 [get_ports clk]
# Virtual clocks: no source object.
create_clock -name vclk_ext -period 13.0
create_clock -name vclk_sys -period 10.0
set_clock_groups -asynchronous -group {clk vclk_sys} -group {vclk_ext}
set_input_delay  -clock vclk_ext 2.0 [get_ports ext_req]
set_input_delay  -clock vclk_sys 2.0 [get_ports cfg_in]
set_output_delay -clock vclk_sys 2.0 [get_ports status]
set_output_delay -clock vclk_ext 2.0 [get_ports ext_ack]
