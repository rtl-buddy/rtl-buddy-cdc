// good_buffered_gated_bus_crossing — mux-on-D gated bus crossing
// with a transparent buffer inserted between the gating mux and the
// destination flops.
//
// Same correctness story as the standard mux-on-D handshake (the
// destination only latches the data when ``load_sync1`` agrees),
// with the fixture's JSON post-processed to splice a ``$_BUF_``
// between the gating mux's ``Y`` output and the destination flops'
// ``D`` pins. CDC-004's shape-2 detector must follow the buffer hop
// and still recognise the originating mux.
//
// The fixture also implements a full req/ack handshake on the src
// side so CDC-012 (functional data-hold) stays silent: the source
// payload is loaded only when arming a new request, held stable
// across the round-trip, and the request clears only when a
// synced-back ack confirms the destination has sampled.
//
// Build (two steps):
//   1) Synthesize the base mux-on-D shape:
//       yosys -q -p 'read_verilog -sv good_buffered_gated_bus_crossing.sv;
//                    hierarchy -top good_buffered_gated_bus_crossing;
//                    proc; flatten; opt_clean;
//                    write_json good_buffered_gated_bus_crossing.json'
//   2) Splice a ``$_BUF_`` between the load mux and each dst-flop
//      D bit:
//       python tests/fixtures/good_buffered_gated_bus_crossing/insert_buffer.py 1
//      The argument is the number of buffer hops to insert (1 for
//      the textbook good case, 3 for the budget-exceeded regression
//      guard in ``test_rule_corners.py``).

module good_buffered_gated_bus_crossing (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        src_load_req,
    input  logic [7:0]  src_data,
    output logic [7:0]  dst_data
);

    // Synced-back ack: src learns when dst has sampled.
    logic dst_ack;
    logic ack_meta, ack_sync;
    always_ff @(posedge src_clk) begin
        ack_meta <= dst_ack;
        ack_sync <= ack_meta;
    end

    // Held request: set on src_load_req, clear on synced-back ack.
    logic       src_load_q;
    always_ff @(posedge src_clk) begin
        if (src_load_req)  src_load_q <= 1'b1;
        else if (ack_sync) src_load_q <= 1'b0;
    end

    // Held payload: load only when arming a new request and no ack
    // is pending. Holds stable across the round-trip.
    logic [7:0] src_q;
    always_ff @(posedge src_clk) begin
        if (src_load_req && !src_load_q && !ack_sync) src_q <= src_data;
    end

    // 2FF synchroniser on the load-enable; ack on the synced enable.
    logic       load_sync0;
    logic       load_sync1;
    always_ff @(posedge dst_clk) begin
        load_sync0 <= src_load_q;
        load_sync1 <= load_sync0;
        dst_ack    <= load_sync1;
    end

    // Mux-on-D gating: select is the dst-domain synced load signal.
    // Yosys lowers the conditional to ``$mux`` + ``$dff``; the
    // post-build rewrite inserts ``$_BUF_`` cells between the mux Y
    // and the flop D pins.
    always_ff @(posedge dst_clk) begin
        dst_data <= load_sync1 ? src_q : dst_data;
    end

endmodule
