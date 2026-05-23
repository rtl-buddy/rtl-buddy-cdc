// Positive counterpart to `bad_sync_chain_foreign_reset` (issue #172).
//
// Structurally identical: a 2FF sync chain in dst_clk samples a
// cross-domain signal from src_clk. The fix versus the bad fixture
// is that the chain's ARST is now sourced from a dst_clk-domain
// reset flop (`dst_rst_q`), so the chain's resolving flops are
// released synchronously with their own clock. The classic
// "async-assert, sync-deassert" reset distribution shape.
//
// CDC-015 must not fire; the chain's reset domain matches its
// clock domain. The crossing-data path is the same single-bit 2FF
// idiom every other good fixture uses, so no other rule fires.

module good_sync_chain_local_reset (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,

    input  logic async_signal,
    output logic dst_q
);

    // Reset registered in dst_clk — i.e. dst_clk's local reset
    // network. (* reset_sync *) tells the reset-domain machinery
    // this is a vetted reset synchroniser, so RDC-* rules don't
    // mis-classify it as a structural reset crossing.
    (* reset_sync *) logic dst_rst_q;
    always_ff @(posedge dst_clk or negedge global_rst_n) begin
        if (!global_rst_n) dst_rst_q <= 1'b0;
        else               dst_rst_q <= 1'b1;
    end

    logic src_q;
    always_ff @(posedge src_clk or negedge global_rst_n) begin
        if (!global_rst_n) src_q <= 1'b0;
        else               src_q <= async_signal;
    end

    // 2FF sync chain in dst_clk — ARST sourced from the dst-domain
    // reset flop. CDC-015 stays silent.
    logic sync_1;
    always_ff @(posedge dst_clk or negedge dst_rst_q) begin
        if (!dst_rst_q) sync_1 <= 1'b0;
        else            sync_1 <= src_q;
    end

    logic sync_2;
    always_ff @(posedge dst_clk or negedge dst_rst_q) begin
        if (!dst_rst_q) sync_2 <= 1'b0;
        else            sync_2 <= sync_1;
    end

    assign dst_q = sync_2;

endmodule
