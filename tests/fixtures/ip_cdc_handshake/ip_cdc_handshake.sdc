# Stub SDC for ip_cdc_handshake CDC test fixture.
#
# Two asynchronous clocks, paired src/dst pulses on the data path. The
# crossing between them is correctly synchronized via 2FF level syncs
# and a req/ack handshake — a clean CDC linter run should report
# zero violations against this constraint set.

# Source domain: 100 MHz
create_clock -name src_clk -period 10.0 [get_ports src_clk]

# Destination domain: ~133 MHz, intentionally not a rational ratio of src_clk
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]

# Mark the two clocks as asynchronous so any unsynchronized register-to-
# register path between them must be flagged by the CDC tool.
set_clock_groups -asynchronous \
    -group {src_clk} \
    -group {dst_clk}
