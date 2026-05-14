# SDC for good_gen_clock_internal_pin (regression fixture for issue #15).
#
# `ck_div` is forwarded from u_c's internal divider via a continuous
# assign on the output port `clk_out`. The gen-clock target is the
# child instance's output port pin — the case the slang frontend was
# originally dropping.

create_clock -name ck_in -period 10.0 [get_ports clk]

create_generated_clock -name ck_div -source [get_ports clk] \
    -master_clock ck_in -divide_by 2 [get_pins u_c/clk_out]

set_input_delay -clock ck_in 0.0 [get_ports d]
