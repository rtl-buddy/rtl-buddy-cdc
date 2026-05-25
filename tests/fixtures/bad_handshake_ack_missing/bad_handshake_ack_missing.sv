// Negative-case fixture for G-5 (handshake reporter refinement,
// rtl-buddy-cdc#214).
//
// Two distinct findings on the same async domain pair:
//   1. CDC-012 fires on the gated multi-bit src→dst bus crossing
//      (no synced-back ack between the two domains).
//   2. CDC-001 fires on a separate unsynced single-bit src→dst
//      crossing (status_q in dst_clk samples status_src_q directly
//      without a 2FF sync chain).
//
// Today the user sees these as unrelated findings; the G-5 reporter
// refinement adds a "[handshake-related]" tag to the CDC-001 message
// pointing at the CDC-012 partner so the missing-ack relationship is
// visible without manual correlation.
//
// Build: standard `proc; opt_dff; flatten;` — opt_dff coerces the
// gated assignment into $dffe (matching CDC-004's shape-1 detector
// and CDC-012's gated-bus precondition).

module bad_handshake_ack_missing (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic [7:0] d_in,
    input  logic       req_in,
    input  logic       status_in,
    output logic [7:0] data_dst,
    output logic       status_q
);

    logic [7:0] data_q;
    logic       req_q;
    logic       status_src_q;
    always_ff @(posedge src_clk) begin
        data_q       <= d_in;
        req_q        <= req_in;
        status_src_q <= status_in;
    end

    logic req_sync_m, req_sync_q;
    always_ff @(posedge dst_clk) begin
        req_sync_m <= req_q;
        req_sync_q <= req_sync_m;
    end

    logic [7:0] data_dst_q;
    always_ff @(posedge dst_clk)
        if (req_sync_q) data_dst_q <= data_q;
    assign data_dst = data_dst_q;

    // Unsynced status signal — single-bit src→dst direct sample.
    always_ff @(posedge dst_clk) status_q <= status_src_q;

endmodule
