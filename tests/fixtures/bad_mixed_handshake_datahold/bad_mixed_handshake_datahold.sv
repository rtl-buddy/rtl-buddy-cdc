// CDC-012 — feedback presence is a crossing-level property, not a
// domain-level one (rtl-buddy-cdc#239).
//
// Two independent multi-bit gated-bus crossings share the same
// src_clk -> dst_clk async pair:
//
//   * Channel A (a_*) is a textbook req/ack handshake — the source
//     holds a_payload until a synced-back ack (a_ack_sync) proves the
//     destination sampled. CDC-012 must stay SILENT on it.
//   * Channel B (b_*) is the broken req-only form — b_payload advances
//     every src_clk while the request crosses, with no ack feedback.
//     CDC-012 must FIRE on it.
//
// The bug this guards against: a feedback cache keyed on the
// (src_clk, dst_clk) domain pair would see channel A's ack feedback
// and silence channel B too. The fix scopes the feedback check to each
// crossing's own source flop, so the broken channel still fires.

module bad_mixed_handshake_datahold (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        a_req,
    input  logic [7:0]  a_data,
    output logic [7:0]  a_dst_data,
    input  logic        b_req,
    input  logic [7:0]  b_data,
    output logic [7:0]  b_dst_data
);

    // ---- Channel A: proper req/ack handshake (CDC-012 silent) ----
    logic a_ack;
    logic a_ack_meta, a_ack_sync;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            a_ack_meta <= 1'b0;
            a_ack_sync <= 1'b0;
        end else begin
            a_ack_meta <= a_ack;
            a_ack_sync <= a_ack_meta;
        end
    end

    logic a_load_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)          a_load_q <= 1'b0;
        else if (a_req)      a_load_q <= 1'b1;
        else if (a_ack_sync) a_load_q <= 1'b0;
    end

    logic [7:0] a_payload;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)
            a_payload <= 8'h00;
        else if (a_req && !a_load_q && !a_ack_sync)
            a_payload <= a_data;
    end

    logic a_load_sync0, a_load_sync1;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            a_load_sync0 <= 1'b0;
            a_load_sync1 <= 1'b0;
            a_ack        <= 1'b0;
            a_dst_data   <= 8'h00;
        end else begin
            a_load_sync0 <= a_load_q;
            a_load_sync1 <= a_load_sync0;
            a_ack        <= a_load_sync1;
            if (a_load_sync1) a_dst_data <= a_payload;
        end
    end

    // ---- Channel B: broken req-only handshake (CDC-012 fires) ----
    logic [7:0] b_payload;
    logic       b_req_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            b_payload <= 8'h00;
            b_req_q   <= 1'b0;
        end else begin
            b_payload <= b_data;
            b_req_q   <= b_req;
        end
    end

    logic b_req_meta, b_req_sync;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            b_req_meta <= 1'b0;
            b_req_sync <= 1'b0;
        end else begin
            b_req_meta <= b_req_q;
            b_req_sync <= b_req_meta;
        end
    end

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n)          b_dst_data <= 8'h00;
        else if (b_req_sync) b_dst_data <= b_payload;
    end

endmodule
