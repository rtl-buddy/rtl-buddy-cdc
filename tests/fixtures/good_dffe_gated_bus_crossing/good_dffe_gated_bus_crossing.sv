// good_dffe_gated_bus_crossing — bus crossing protected by a
// $dffe-style load enable.
//
// Same correctness story as the mux-on-D handshake (the destination
// only samples src_data when a synchronized control signal allows
// it), but the gating is expressed via an enabled flop instead of a
// load mux. The destination cell is a $dffe whose EN pin is the
// dst-side output of a 2FF synchronizer on the load-request bit;
// CDC-004's shape-1 detector must accept this as gated.
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

    logic [7:0] src_q;
    logic       src_load_q;     // src-side registered copy of req
    logic       load_sync0;
    logic       load_sync1;

    // Register the source-side request so the synchronizer sees a
    // properly registered driver (otherwise CDC-006 would fire on
    // the unregistered top-level port reaching the synced flop's D).
    always_ff @(posedge src_clk) begin
        src_q      <= src_data;
        src_load_q <= src_load_req;
    end

    // 2FF synchronizer on the load-enable control bit. Sits entirely
    // in the dst domain after the first stage.
    always_ff @(posedge dst_clk) begin
        load_sync0 <= src_load_q;
        load_sync1 <= load_sync0;
    end

    // GOOD: the destination flop is a $dffe whose EN is driven by the
    // dst-domain synced load signal. Yosys (with opt_dff) lowers
    // this to ``$dffe`` with EN = load_sync1, so the bus is only
    // sampled on cycles where the source has signaled a stable
    // transfer.
    always_ff @(posedge dst_clk) begin
        if (load_sync1) dst_data <= src_q;
    end

endmodule
