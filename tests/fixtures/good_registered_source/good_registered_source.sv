// Positive counterpart to bad_comb_source.
//
// In the bad version, top-level combinational inputs `a` and `b` are
// AND'd straight into the dst_clk synchronizer with no source-domain
// flop — the comb output can glitch and the sync may sample it.
// Fix: register the AND in a source clock domain *first*, then run
// the standard 2FF synchronizer. CDC-006 only fires when the sync's
// fanin reaches a top-level port without a registering flop; here
// the fanin terminates at src_anded, so it stays silent.

module good_registered_source (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic a,
    input  logic b,
    output logic q_out
);

    logic src_anded;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_anded <= 1'b0;
        else        src_anded <= a & b;
    end

    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_anded;
    end

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
