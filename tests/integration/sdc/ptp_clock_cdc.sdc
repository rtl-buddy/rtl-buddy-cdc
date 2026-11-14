# alexforencich/verilog-ethernet ptp_clock_cdc (PTP time-of-day clock
# transfer between clock domains), default parameters. Written for
# rtl-buddy-cdc's integration corpus.
#
# Three unrelated sources: the source PTP clock (input_clk), the
# destination clock (output_clk) and the phase-detector sample clock
# (sample_clk). input_rst / output_rst are synchronous resets, typed
# like data inputs.
create_clock -name input_clk  -period 8.0  [get_ports input_clk]
create_clock -name output_clk -period 6.4  [get_ports output_clk]
create_clock -name sample_clk -period 9.0  [get_ports sample_clk]
set_clock_groups -asynchronous -group {input_clk} -group {output_clk} \
    -group {sample_clk}

set_input_delay  -clock input_clk  1.0 [get_ports {input_rst input_ts input_ts_step}]
set_input_delay  -clock output_clk 1.0 [get_ports {output_rst}]
set_output_delay -clock output_clk 1.0 [get_ports {output_ts output_ts_step \
    output_pps locked}]
