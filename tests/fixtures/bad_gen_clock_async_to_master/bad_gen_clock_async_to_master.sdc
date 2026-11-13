create_clock -name clk -period 10.0 [get_ports clk]
create_generated_clock -name ck_div2 -master_clock clk \
    -source [get_ports clk] -divide_by 2 [get_pins div2_q]
create_generated_clock -name ck_div4 -master_clock ck_div2 \
    -source [get_pins div2_q] -divide_by 2 [get_pins div4_q]
# The divider's reset release is not aligned to clk, so the design
# treats ck_div2 as asynchronous to its own master.
set_clock_groups -asynchronous -group {clk} -group {ck_div2}
set_input_delay -clock clk     1.0 [get_ports d_a]
set_input_delay -clock ck_div2 1.0 [get_ports d_2]
