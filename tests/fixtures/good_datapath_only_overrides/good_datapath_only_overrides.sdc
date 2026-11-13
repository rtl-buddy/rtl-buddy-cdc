create_clock -name clk   -period 10.0 [get_ports clk]
create_generated_clock -name clk_half -master_clock clk \
    -source [get_ports clk] -divide_by 2 [get_ports clk_half]
create_clock -name ck_m0 -period  8.0 [get_ports ck_m0]
create_clock -name ck_m1 -period  5.0 [get_ports ck_m1]
set_clock_groups -physically_exclusive -group {ck_m0} -group {ck_m1}
set_input_delay -clock clk   1.0 [get_ports d_a]
set_input_delay -clock ck_m0 1.0 [get_ports d_m0]
# Datapath-only bounds: each pair is an intended asynchronous crossing.
set_max_delay 4.0 -datapath_only \
    -from [get_clocks clk] -to [get_clocks clk_half]
set_max_delay 3.0 -datapath_only \
    -from [get_clocks ck_m0] -to [get_clocks ck_m1]
