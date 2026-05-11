create_clock -name ck_a -period 10.0 [get_ports ck_a]

# Forwarded clocks originating inside the design. Each is declared at
# the internal pin where the new clock takes over and is rooted (via
# -master_clock) at ck_a, so resolve() collapses every flop's domain
# back to ck_a and ck_a↔ck_b0 (etc.) are synchronous.
create_generated_clock -name ck_b0 -source [get_ports ck_a] \
    -master_clock ck_a [get_pins u_a/clk_out_b0]
create_generated_clock -name ck_b1 -source [get_ports ck_a] \
    -master_clock ck_a [get_pins u_a/clk_out_b1]
create_generated_clock -name ck_c0 -source [get_pins u_a/clk_out_b0] \
    -master_clock ck_b0 [get_pins u_b0/clk_out]
create_generated_clock -name ck_c1 -source [get_pins u_a/clk_out_b1] \
    -master_clock ck_b1 [get_pins u_b1/clk_out]

set_input_delay -clock ck_a 0.0 [get_ports d_in]
