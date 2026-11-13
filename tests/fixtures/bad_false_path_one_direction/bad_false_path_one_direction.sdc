create_clock -name ck_a -period 10.0 [get_ports ck_a]
create_clock -name ck_b -period  7.5 [get_ports ck_b]
set_input_delay -clock ck_a 1.0 [get_ports d_a]
set_input_delay -clock ck_b 1.0 [get_ports d_b]
# No clock groups. One direction only: ck_a → ck_b.
set_false_path -from [get_clocks ck_a] -to [get_clocks ck_b]
