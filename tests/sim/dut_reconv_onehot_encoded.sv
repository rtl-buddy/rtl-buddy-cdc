// CDC-005 good DUT (rtl-buddy-cdc#331): the fix for
// `dut_reconv_onehot.sv` — encode the one-hot state as the one bit
// that carries its information (`sel`: 0 = 01, 1 = 10), cross that
// single register through one 2FF synchroniser, and decode back to
// one-hot inside dst_clk. A one-bit crossing has nothing to
// reconverge with, so the decoded pair is always 01 or 10.
//
// The analyzer sees one synchronised single-bit crossing: every rule
// must stay silent. The simulation must never show 00 or 11.

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_reconv_onehot_encoded (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [1:0] q_out
);
    logic sel = 1'b0;
    always_ff @(posedge src_clk) if (inc) sel <= ~sel;

    // The one-hot state the source represents; the testbench reads it
    // as `u_dut.src_data`. Decoded in src_clk for observation only.
    wire [1:0] src_data = {sel, ~sel};

    logic s_meta;
    logic s_sync = 1'b0;
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0041))
        u_s_meta (.clk(dst_clk), .d(sel), .q(s_meta));
    always_ff @(posedge dst_clk) s_sync <= s_meta;

    // Decode after the synchroniser.
    logic [1:0] pair_q = 2'b01;
    always_ff @(posedge dst_clk) pair_q <= {s_sync, ~s_sync};

    assign q_out = pair_q;
endmodule
