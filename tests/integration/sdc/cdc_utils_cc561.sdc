# fusesoc/cdc_utils cc561 (multi-cycle path formulation with a
# synchronised enable pulse), elaborated through wrappers/cc561_top.v
# with DW=8. Written for rtl-buddy-cdc's integration corpus.
#
# aclk / bclk are unrelated sources. arst is a synchronous reset in
# aclk, typed like a data input.
create_clock -name aclk -period 10.0 [get_ports aclk]
create_clock -name bclk -period 12.5 [get_ports bclk]
set_clock_groups -asynchronous -group {aclk} -group {bclk}

set_input_delay  -clock aclk 1.0 [get_ports {arst adata aen}]
set_output_delay -clock bclk 1.0 [get_ports {bdata ben}]
