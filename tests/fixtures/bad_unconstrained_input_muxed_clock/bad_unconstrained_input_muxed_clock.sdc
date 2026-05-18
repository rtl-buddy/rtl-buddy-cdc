create_clock -name tclk -period 10.0 [get_ports tclk]
create_clock -name sclk -period  7.5 [get_ports sclk]
set_clock_groups -asynchronous -group {tclk} -group {sclk}
# Intentionally NO `set_input_delay` on `d[3:0]` — CDC-011 (#97)
# surfaces the missing constraint. `tm` is a mode pin; left untyped
# here because it never reaches a flop's D pin (it only fans into
# the clock mux's S input), so CDC-011 doesn't trip on it.
