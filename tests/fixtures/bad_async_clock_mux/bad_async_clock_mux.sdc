create_clock -name ck0 -period 10.0  [get_ports {ck0_a ck0_b}]
create_clock -name ck1 -period 13.3  [get_ports ck1]
set_clock_groups -asynchronous -group {ck0} -group {ck1}
# Type the data ports so CDC-011 stays silent — the rule under
# test is CDC-010, not the unconstrained-input rule.
set_input_delay -clock ck1 0.0 [get_ports sel_d]
set_input_delay -clock ck0 0.0 [get_ports d_in]
