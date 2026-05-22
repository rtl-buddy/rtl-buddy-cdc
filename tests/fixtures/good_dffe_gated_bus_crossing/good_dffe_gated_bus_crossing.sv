// good_dffe_gated_bus_crossing — bus crossing protected by a
// $dffe-style load enable.
//
// The dst-side gating is a $dffe whose EN is the dst-side output of
// a 2FF synchroniser on the load-request bit. CDC-004's shape-1
// detector accepts this as gated.
//
// The fixture also implements a full req/ack handshake on the src
// side so CDC-012 (functional data-hold) stays silent: the source
// payload is loaded only when arming a new request, held stable
// across the round-trip, and the request clears only when a
// synced-back ack confirms the destination has sampled. Without
// the ack feedback, the payload could change between when the
// request is registered and when the destination latches it — the
// CDC-012 failure mode.
//
// Build:
//   yosys -p 'read_verilog -sv good_dffe_gated_bus_crossing.sv;
//             hierarchy -top good_dffe_gated_bus_crossing;
//             proc; opt_dff; flatten;
//             write_json good_dffe_gated_bus_crossing.json'
// The non-default ``opt_dff`` pass coerces Yosys to fold the
// hold-mux back into a ``$dffe`` cell. Without it Yosys leaves the
// design as ``$dff`` + ``$mux``, which the CDC-004 shape-2 detector
// would also accept — defeating the fixture's purpose.

module good_dffe_gated_bus_crossing (
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

    // GOOD: $dffe-style EN gating on the dst side. Yosys (with
    // opt_dff) lowers this to ``$dffe`` with EN = load_sync1.
    always_ff @(posedge dst_clk) begin
        if (load_sync1) dst_data <= src_q;
    end

endmodule
