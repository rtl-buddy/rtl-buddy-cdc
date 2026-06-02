// Positive counterpart to bad_mixed_handshake_datahold (CDC-012,
// rtl-buddy-cdc#239).
//
// Two independent multi-bit gated-bus crossings share the same
// src_clk -> dst_clk async pair, and BOTH are textbook req/ack
// handshakes — each source holds its payload until a synced-back ack
// proves the destination has sampled. CDC-012 must stay silent on
// both channels.
//
// Paired with the bad fixture, this pins the per-crossing feedback
// scoping from both sides: the bad fixture proves a broken channel
// still fires next to a good one; this proves two good channels in
// the same domain pair don't false-fire on each other.

module good_mixed_handshake_datahold (
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

    // ---- Channel A: proper req/ack handshake ----
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

    // ---- Channel B: proper req/ack handshake ----
    logic b_ack;
    logic b_ack_meta, b_ack_sync;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            b_ack_meta <= 1'b0;
            b_ack_sync <= 1'b0;
        end else begin
            b_ack_meta <= b_ack;
            b_ack_sync <= b_ack_meta;
        end
    end

    logic b_load_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)          b_load_q <= 1'b0;
        else if (b_req)      b_load_q <= 1'b1;
        else if (b_ack_sync) b_load_q <= 1'b0;
    end

    logic [7:0] b_payload;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)
            b_payload <= 8'h00;
        else if (b_req && !b_load_q && !b_ack_sync)
            b_payload <= b_data;
    end

    logic b_load_sync0, b_load_sync1;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            b_load_sync0 <= 1'b0;
            b_load_sync1 <= 1'b0;
            b_ack        <= 1'b0;
            b_dst_data   <= 8'h00;
        end else begin
            b_load_sync0 <= b_load_q;
            b_load_sync1 <= b_load_sync0;
            b_ack        <= b_load_sync1;
            if (b_load_sync1) b_dst_data <= b_payload;
        end
    end

endmodule
