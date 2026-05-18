// Negative-case fixture for RDC-003 — sync reset crossing without a
// reset synchroniser.
//
// `src_rst` is registered in `src_clk` and used directly as the
// **synchronous** reset (`SRST`) of `q_dst` in `dst_clk`. Sync
// resets are sampled on the destination clock's edge — if the
// upstream signal lives in a foreign async clock domain, the sample
// may be metastable on the cycle the source flop changes.
//
// The classic fix is a 2FF reset synchroniser in `dst_clk` between
// `src_rst` and the consuming flop. The paired `good_*` counterpart
// shows it.
//
// RDC-003 must fire once on `q_dst`; no other rule should fire
// (q_dst's D = a port, so no data crossing).
//
// Build:
//   yosys -p 'read_verilog -sv bad_rdc_003_sync_reset_crossing.sv;
//             hierarchy -top bad_rdc_003_sync_reset_crossing;
//             proc; opt_dff; flatten; opt_clean;
//             write_json bad_rdc_003_sync_reset_crossing.json'
//
// `opt_dff` is required to fold the mux-on-D shape into `$sdff` —
// without it the consumer is a plain `$dff` with a `$mux` selecting
// between `1'b0` and `d_in` based on `src_rst`, and RDC-003 (which
// keys on the `SRST` pin) cannot recognise the sync-reset path.

module bad_rdc_003_sync_reset_crossing (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic kill_req,
    input  logic d_in,
    output logic q_out
);

    logic src_rst;
    always_ff @(posedge src_clk or negedge global_rst_n) begin
        if (!global_rst_n) src_rst <= 1'b0;
        else               src_rst <= kill_req;
    end

    // Sync-reset consumer: NO async-reset edge in the sensitivity
    // list, so Yosys infers `$sdff` with SRST driven by `src_rst`.
    logic q_dst;
    always_ff @(posedge dst_clk) begin
        if (src_rst) q_dst <= 1'b0;
        else         q_dst <= d_in;
    end

    assign q_out = q_dst;

endmodule
