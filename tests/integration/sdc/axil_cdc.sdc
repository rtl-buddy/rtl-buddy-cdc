# alexforencich/verilog-axi axil_cdc (AXI-lite clock-domain crossing),
# default parameters. Written for rtl-buddy-cdc's integration corpus.
#
# s_* ports belong to s_clk, m_* ports to m_clk, unrelated sources.
# s_rst / m_rst are synchronous resets, typed like data inputs.
create_clock -name s_clk -period 10.0 [get_ports s_clk]
create_clock -name m_clk -period 12.5 [get_ports m_clk]
set_clock_groups -asynchronous -group {s_clk} -group {m_clk}

set_input_delay -clock s_clk 1.0 [get_ports {s_rst s_axil_awaddr s_axil_awprot \
    s_axil_awvalid s_axil_wdata s_axil_wstrb s_axil_wvalid s_axil_bready \
    s_axil_araddr s_axil_arprot s_axil_arvalid s_axil_rready}]
set_output_delay -clock s_clk 1.0 [get_ports {s_axil_awready s_axil_wready \
    s_axil_bresp s_axil_bvalid s_axil_arready s_axil_rdata s_axil_rresp \
    s_axil_rvalid}]
set_input_delay -clock m_clk 1.0 [get_ports {m_rst m_axil_awready m_axil_wready \
    m_axil_bresp m_axil_bvalid m_axil_arready m_axil_rdata m_axil_rresp \
    m_axil_rvalid}]
set_output_delay -clock m_clk 1.0 [get_ports {m_axil_awaddr m_axil_awprot \
    m_axil_awvalid m_axil_wdata m_axil_wstrb m_axil_wvalid m_axil_bready \
    m_axil_araddr m_axil_arprot m_axil_arvalid m_axil_rready}]
