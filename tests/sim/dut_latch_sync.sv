// G-1 sim case: "synchroniser" built from a transparent latch.
//
// The dst-domain receiver is a real meta_flop, but the upstream
// stage that's supposed to absorb metastability is a $dlatch. While
// latch_en is high, the latch is transparent — metastable src_q
// values propagate straight through to the meta_flop's D pin.
//
// Expected: sim produces a non-zero error rate (the latch can't
// resolve metastability the way a flop chain can).
//
// Cross-reference with the analyzer: rules.run_all returns ZERO
// findings on the Yosys netlist of this DUT (find_crossings doesn't
// trace through the latch). The pair is the gap-mining payoff: sim
// fails, analyzer silent.

`include "meta_flop_lib.sv"

module dut_latch_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);
    // Source flop — its D is dst-synchronous (driven by the TB).
    logic src_q;
    safe_flop #(.SEED(32'hA5A5_0001))
        u_src (.clk(src_clk), .d(d_in), .q(src_q));

    // Latch-pretending-to-be-a-sync-stage. Use dst_clk's level as
    // the enable so the latch is transparent half the time on the
    // destination side — the worst case for a designer who thought
    // they were building a 2FF.
    logic latch_q;
    always_latch
        if (dst_clk) latch_q = src_q;

    // Destination flop — its D should be a clean dst-synchronous
    // signal but receives the latch's transparent passthrough,
    // potentially during a metastable transition.
    meta_flop #(.RATE_PCT(80), .SEED(32'h1234_5678))
        u_dst (.clk(dst_clk), .d(latch_q), .q(q_out));
endmodule
