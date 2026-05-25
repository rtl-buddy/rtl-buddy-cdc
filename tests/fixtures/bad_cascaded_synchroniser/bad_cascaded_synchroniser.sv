// Negative-case fixture for CDC-018.
//
// A dst-domain sync chain whose depth (4 flops) exceeds the
// textbook 2FF minimum. The chain still works (extra latency,
// slightly worse MTBF tail), but the depth is a code-review smell
// — classically caused by two engineers each adding their own
// sync chain on the same wire, or a refactor that left the
// original chain in place when a new wrapper was added.
//
// CDC-018 must fire once on the (src_q, dst_clk) group. CDC-001 /
// CDC-002 stay silent because the chain is structurally
// well-formed at depth >= required_depth.

module bad_cascaded_synchroniser (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    logic meta, sync_q, sync2_meta, sync2_q;
    always_ff @(posedge dst_clk) begin
        meta       <= src_q;
        sync_q     <= meta;
        sync2_meta <= sync_q;
        sync2_q    <= sync2_meta;
    end

    assign q_out = sync2_q;

endmodule
