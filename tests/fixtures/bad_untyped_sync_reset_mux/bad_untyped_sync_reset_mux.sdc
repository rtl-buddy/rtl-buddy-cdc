create_clock -name clk -period 10.0 [get_ports clk]
# Intentionally NO `set_input_delay` on `srst` or `dctl` — the missing
# typing on the sync reset is exactly the gap rtl-buddy-cdc#272 is
# about. Adding it here would invalidate the fixture (see the paired
# `good_typed_sync_reset`).
