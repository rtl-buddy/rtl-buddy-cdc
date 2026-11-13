# alexforencich/verilog-axis axis_async_fifo, default parameters.
# Written for rtl-buddy-cdc's integration corpus.
#
# s_* ports belong to s_clk, m_* ports to m_clk, unrelated sources.
# s_rst / m_rst are synchronous resets in this design (sampled by the
# owning clock), so they are typed like data inputs.
create_clock -name s_clk -period 10.0 [get_ports s_clk]
create_clock -name m_clk -period 12.5 [get_ports m_clk]
set_clock_groups -asynchronous -group {s_clk} -group {m_clk}

set_input_delay -clock s_clk 1.0 [get_ports {s_rst s_axis_tdata s_axis_tkeep \
    s_axis_tvalid s_axis_tlast s_axis_tid s_axis_tdest s_axis_tuser s_pause_req}]
set_output_delay -clock s_clk 1.0 [get_ports {s_axis_tready s_pause_ack \
    s_status_depth s_status_depth_commit s_status_overflow s_status_bad_frame \
    s_status_good_frame}]
set_input_delay -clock m_clk 1.0 [get_ports {m_rst m_axis_tready m_pause_req}]
set_output_delay -clock m_clk 1.0 [get_ports {m_axis_tdata m_axis_tkeep \
    m_axis_tvalid m_axis_tlast m_axis_tid m_axis_tdest m_axis_tuser m_pause_ack \
    m_status_depth m_status_depth_commit m_status_overflow m_status_bad_frame \
    m_status_good_frame}]
