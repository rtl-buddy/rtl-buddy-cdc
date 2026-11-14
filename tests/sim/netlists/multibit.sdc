# Shared constraints of the multi-bit sim-oracle DUTs
# (tests/sim/dut_bus_*.sv, tests/sim/dut_reconv_*.sv; rtl-buddy-cdc#331).
# The periods match tb_bus_crossing.sv's defaults; the testbench varies
# them per run, the analyzer only needs the two clocks to be async.
create_clock -name src_clk -period 20.0 [get_ports src_clk]
create_clock -name dst_clk -period 15.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports inc]
