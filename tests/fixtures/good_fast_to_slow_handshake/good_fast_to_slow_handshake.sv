// Positive counterpart to bad_fast_to_slow_control_loss (CDC-013).
//
// Same fast-to-slow clock ratio (src_clk 2.0ns, dst_clk 20.0ns), and
// the same goal of forwarding a single event from the fast domain to
// the slow domain. Where the bad fixture toggles a level and risks
// losing event accounting when two events occur between dst samples,
// this one holds the request until the destination acknowledges it
// (synced back through a 2FF chain). The source's enable that
// re-arms a new request waits for an ack, so a second event cannot
// be issued before the first has been observed.
//
// CDC-013 must NOT fire — the src request flop's D is a priority-
// encoded $mux nest (one branch sets on req_in, another clears on
// synced ack, the default holds), not the Q/~Q toggle shape the
// classifier matches.
//
// Two async crossings (the synced-back ack and the held request).

module good_fast_to_slow_handshake (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic event_in,
    output logic event_seen
);

    // Synced-back ack: dst_ack synchronised into the src domain so
    // src can tell when the destination has observed the request.
    logic ack_meta, ack_sync;
    logic dst_ack;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            ack_meta <= 1'b0;
            ack_sync <= 1'b0;
        end else begin
            ack_meta <= dst_ack;
            ack_sync <= ack_meta;
        end
    end

    // Held request: set on event_in, cleared only when ack returns.
    // The D pin of this flop is a priority-encoded $mux nest, not the
    // Q/~Q toggle pattern CDC-013 looks for.
    logic req_held;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)        req_held <= 1'b0;
        else if (event_in) req_held <= 1'b1;
        else if (ack_sync) req_held <= 1'b0;
    end

    // Destination side: 2FF synchroniser, ack the request, pulse the
    // event_seen output on the request's rising edge in dst_clk.
    logic req_meta, req_sync, req_sync_d;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            req_meta   <= 1'b0;
            req_sync   <= 1'b0;
            req_sync_d <= 1'b0;
            dst_ack    <= 1'b0;
            event_seen <= 1'b0;
        end else begin
            req_meta   <= req_held;
            req_sync   <= req_meta;
            req_sync_d <= req_sync;
            dst_ack    <= req_sync;
            event_seen <= req_sync & ~req_sync_d;
        end
    end

endmodule
