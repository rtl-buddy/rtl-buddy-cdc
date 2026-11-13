create_clock -name clk     -period 10.0 [get_ports clk]
create_clock -name clk_ext -period  7.0 [get_ports clk_ext]
# A generated clock of a generated clock: clk → ck_div2 → ck_div4.
create_generated_clock -name ck_div2 -master_clock clk \
    -source [get_ports clk] -divide_by 2 [get_pins div2_q]
create_generated_clock -name ck_div4 -master_clock ck_div2 \
    -source [get_pins div2_q] -divide_by 2 [get_pins div4_q]
# The group names the roots only; generated clocks follow their root.
set_clock_groups -asynchronous -group {clk} -group {clk_ext}
set_input_delay -clock clk     1.0 [get_ports d_a]
set_input_delay -clock clk_ext 1.0 [get_ports d_ext]
