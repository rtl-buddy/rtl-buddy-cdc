// Negative fixture for CDC-017 — transparent latch in CDC path.
//
// A designer who tried to build a synchroniser out of a $dlatch +
// flop pair. During the latch's enable-active phase, the foreign
// domain src_q value propagates transparently through the latch to
// the dst-clock flop's D pin — no metastability resolution at all.
//
// find_crossings doesn't traverse latches (it keys off flop-flop
// fanin), so without CDC-017 the entire bug is silent: zero
// crossings, zero findings on a real CDC failure. This fixture is
// the structural proof that the rule catches what the crossing
// walker can't see.

module bad_latch_in_cdc_path (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic latch_en,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    // The latch — designer's "first sync stage". Transparent while
    // latch_en is high.
    logic latch_q;
    always_latch
        if (latch_en) latch_q = src_q;

    // Real dst-domain flop captures the latch output.
    logic sync_q;
    always_ff @(posedge dst_clk) sync_q <= latch_q;

    assign q_out = sync_q;

endmodule
