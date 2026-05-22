// Positive counterpart to bad_functional_datahold_enable (CDC-012).
//
// Same gated-bus crossing topology — src registers a payload and a
// request, dst syncs the request and captures the payload on the
// synced enable — but with the data-hold issue fixed via a textbook
// req/ack handshake:
//
//   - dst_ack is set when the destination has sampled the payload.
//   - dst_ack is 2FF-synced back into the src domain (ack_sync).
//   - src_load_q is set on src_req and cleared only on ack_sync.
//   - src_payload is loaded only when *arming* a new request
//     (src_req high, no current request in flight, no ack pending);
//     once armed, it holds stable across the entire round-trip.
//
// CDC-012 must NOT fire — the existence of a src-domain flop
// (ack_meta / ack_sync) whose D-pin fanin reaches a dst-domain
// flop's Q (dst_ack) is the structural marker of the handshake.
// CDC-004 stays silent for the same reason as bad fixture: the dst
// data flop is a $dffe gated by load_sync1.

module good_functional_datahold_handshake (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        src_req,
    input  logic [7:0]  src_data,
    output logic [7:0]  dst_data
);

    // Synced-back ack: src learns when dst has sampled.
    logic dst_ack;
    logic ack_meta, ack_sync;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            ack_meta <= 1'b0;
            ack_sync <= 1'b0;
        end else begin
            ack_meta <= dst_ack;
            ack_sync <= ack_meta;
        end
    end

    // Held request: set on src_req, cleared only when ack returns.
    logic src_load_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)         src_load_q <= 1'b0;
        else if (src_req)   src_load_q <= 1'b1;
        else if (ack_sync)  src_load_q <= 1'b0;
    end

    // Held payload: load only when arming a new request and the
    // previous one has been acked. The payload is then stable for
    // the entire round-trip — dst always samples a matching value.
    logic [7:0] src_payload;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)
            src_payload <= 8'h00;
        else if (src_req && !src_load_q && !ack_sync)
            src_payload <= src_data;
    end

    // 2FF sync of req into dst; ack on the synced enable.
    logic load_sync0, load_sync1;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            load_sync0 <= 1'b0;
            load_sync1 <= 1'b0;
            dst_ack    <= 1'b0;
            dst_data   <= 8'h00;
        end else begin
            load_sync0 <= src_load_q;
            load_sync1 <= load_sync0;
            dst_ack    <= load_sync1;
            if (load_sync1) dst_data <= src_payload;
        end
    end

endmodule
