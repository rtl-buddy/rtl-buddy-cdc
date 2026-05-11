create_clock -name ck_a -period 10.0 [get_ports ck_a]

# Same internal-pin generated-clock declarations as the good fixture,
# so each block's flops are labelled with its block-specific clock.
create_generated_clock -name ck_b0 -source [get_ports ck_a] \
    -master_clock ck_a [get_pins u_a/clk_out_b0]
create_generated_clock -name ck_b1 -source [get_ports ck_a] \
    -master_clock ck_a [get_pins u_a/clk_out_b1]
create_generated_clock -name ck_c0 -source [get_pins u_a/clk_out_b0] \
    -master_clock ck_b0 [get_pins u_b0/clk_out]
create_generated_clock -name ck_c1 -source [get_pins u_a/clk_out_b1] \
    -master_clock ck_b1 [get_pins u_b1/clk_out]

# The methodology bug: integration SDC declares all five clocks
# pairwise asynchronous, overriding the master chain. ``are_async``
# honours this unresolved-name override, so each source-sync link
# becomes a CDC-001 violation.
set_clock_groups -asynchronous \
    -group {ck_a} \
    -group {ck_b0} \
    -group {ck_b1} \
    -group {ck_c0} \
    -group {ck_c1}

set_input_delay -clock ck_a 0.0 [get_ports d_in]
