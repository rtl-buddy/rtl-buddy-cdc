create_clock -name ck_a -period 10.0 [get_ports ck_a]
create_clock -name ck_b -period  7.5 [get_ports ck_b]
set_input_delay -clock ck_a 1.0 [get_ports d_in]
# No set_clock_groups -asynchronous / set_false_path in this file — the
# only statement relating the two clocks is a datapath-only max delay,
# which marks the pair as an intended async crossing (#325).
set_max_delay 5.0 -datapath_only -from [get_clocks ck_a] -to [get_clocks ck_b]
