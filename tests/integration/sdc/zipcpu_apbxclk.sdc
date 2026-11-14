# ZipCPU/wb2axip apbxclk (APB clock-domain crossing bridge), default
# parameters. Written for rtl-buddy-cdc's integration corpus.
#
# S_* ports belong to S_APB_PCLK, M_* ports to M_APB_PCLK, unrelated
# sources. S_PRESETn is an asynchronous reset input and stays untyped.
create_clock -name S_APB_PCLK -period 10.0 [get_ports S_APB_PCLK]
create_clock -name M_APB_PCLK -period 12.5 [get_ports M_APB_PCLK]
set_clock_groups -asynchronous -group {S_APB_PCLK} -group {M_APB_PCLK}

set_input_delay -clock S_APB_PCLK 1.0 [get_ports {S_APB_PSEL S_APB_PENABLE \
    S_APB_PADDR S_APB_PWRITE S_APB_PWDATA S_APB_PWSTRB S_APB_PPROT}]
set_output_delay -clock S_APB_PCLK 1.0 [get_ports {S_APB_PREADY S_APB_PRDATA \
    S_APB_PSLVERR}]
set_input_delay -clock M_APB_PCLK 1.0 [get_ports {M_APB_PREADY M_APB_PRDATA \
    M_APB_PSLVERR}]
set_output_delay -clock M_APB_PCLK 1.0 [get_ports {M_PRESETn M_APB_PSEL \
    M_APB_PENABLE M_APB_PADDR M_APB_PWRITE M_APB_PWDATA M_APB_PWSTRB \
    M_APB_PPROT}]
