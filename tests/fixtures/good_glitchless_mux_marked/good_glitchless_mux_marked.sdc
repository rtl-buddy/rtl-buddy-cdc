create_clock -name ck0 -period 10.0 [get_ports {ck0_a ck0_b}]
create_clock -name ck1 -period 7.5  [get_ports ck1]
set_clock_groups -asynchronous -group {ck0} -group {ck1}
set_input_delay -clock ck1 1.0 [get_ports sel_d]
set_input_delay -clock ck0 1.0 [get_ports d_in]
