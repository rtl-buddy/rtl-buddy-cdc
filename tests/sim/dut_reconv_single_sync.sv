// CDC-005 good DUT (rtl-buddy-cdc#331): the fix for
// `dut_reconv_fanout.sv` — synchronise the source bit once, then fan
// the synchronised value out inside dst_clk. Both consumers read the
// same resolved bit, so the recombination register can only see the
// pairs (0, 0) and (1, 1) the source presents.
//
// The analyzer sees a single synchronised crossing: CDC-005 (and
// every other rule) must stay silent. The simulation must never show
// a mismatched pair.

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_reconv_single_sync (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [1:0] q_out
);
    logic src_q = 1'b0;
    always_ff @(posedge src_clk) if (inc) src_q <= ~src_q;

    // What the source presents; the testbench reads `u_dut.src_data`.
    wire [1:0] src_data = {src_q, src_q};

    logic s_meta;
    logic s_sync = 1'b0;
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0021))
        u_s_meta (.clk(dst_clk), .d(src_q), .q(s_meta));
    always_ff @(posedge dst_clk) s_sync <= s_meta;

    // Fan-out after the synchroniser: both consumers see one value.
    logic a_use, b_use;
    assign a_use = s_sync;
    assign b_use = s_sync;

    logic [1:0] pair_q = 2'b00;
    always_ff @(posedge dst_clk) pair_q <= {a_use, b_use};

    assign q_out = pair_q;
endmodule
