// Trust-boundary sentinel (issue #178): `(* cdc_sync *)` placed on
// the SOURCE-side producer flop, not the destination sync first
// stage. The attribute should be a no-op for CDC-001 suppression —
// the rule looks for the marker on the *destination* flop of a
// crossing (`c.dst_flop.cell.name in ctx.user_syncs`).
//
// Structurally a 1FF "chain": src_q → dst_q with no second stage in
// dst_clk. CDC-001 must still fire on dst_q despite the (mis-placed)
// attribute on src_q.

module bad_marked_user_sync_src_flop (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    // User mistakenly marks the *source* flop, hoping CDC-001 will
    // treat the crossing as vetted. Suppression is dst-flop-keyed,
    // so this annotation does not apply.
    (* cdc_sync = "level_2ff" *) logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // No second stage — depth 1. CDC-001 fires here.
    logic dst_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
