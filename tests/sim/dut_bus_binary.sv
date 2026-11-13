// CDC-004 bad DUT (rtl-buddy-cdc#331): a 4-bit binary counter crosses
// into dst_clk through a 4-bit 2FF synchroniser — every lane its own
// 2FF, nothing keeping the lanes together. A binary increment can
// change several bits at once (0111 -> 1000); the lanes of the first
// stage resolve independently, so the destination can capture a mix
// of old and new bits that the counter never held.
//
// The analyzer sees a 4-bit crossing with no gating and a source that
// is not gray-coded: CDC-004 must fire. The simulation must show the
// incoherent samples (`tb_bus_crossing.sv`'s scoreboard).

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_bus_binary (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [3:0] q_out
);
    // Source register; the testbench reads it as `u_dut.src_data`.
    logic [3:0] src_data = 4'd0;
    always_ff @(posedge src_clk) if (inc) src_data <= src_data + 4'd1;

    logic [3:0] sync_meta;
    meta_flop_bus #(.WIDTH(4), .RATE_PCT(80), .SEED(32'hB1_0001))
        u_meta (.clk(dst_clk), .d(src_data), .q(sync_meta));

    logic [3:0] sync_q = 4'd0;
    always_ff @(posedge dst_clk) sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
