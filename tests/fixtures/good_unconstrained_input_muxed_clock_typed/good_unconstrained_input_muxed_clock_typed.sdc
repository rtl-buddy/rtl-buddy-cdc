create_clock -name tclk -period 10.0 [get_ports tclk]
create_clock -name sclk -period  7.5 [get_ports sclk]
set_clock_groups -asynchronous -group {tclk} -group {sclk}
# `d[3:0]` is typed against whichever side of the mux trace_clock_root
# resolves to. Here Yosys' conditional-operator lowering puts `sclk`
# on the mux's A arm (the "false" branch of `tm ? tclk : sclk`), so
# the trace's `for in_port in ("A", "B")` walk returns sclk. CDC-011
# silent (typed), CDC-001 silent (src==dst once resolved).
set_input_delay -clock sclk 1.0 [get_ports d]
