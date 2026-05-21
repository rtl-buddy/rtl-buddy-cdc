// Coverage for `(* cdc_sync *)` suppressing CDC-001 on a single dst
// flop.
//
// Structurally identical to `bad_single_ff_sync` — one source flop, a
// single destination flop, no second-stage synchronizer — but the
// destination flop's netname carries `(* cdc_sync = "level_2ff" *)`.
// The marker is the user asserting "this is a vetted synchronizer
// first stage even though the structural depth is 1"; CDC-001/-002/-003
// must skip it.
//
// Paired with `bad_single_ff_sync` (must fire) and with
// `marked_user_sync` (the marker on a conventional 2FF chain).

module marked_single_ff_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // (* cdc_sync *) marks this single dst flop as a user-declared
    // synchronizer first stage. Without the attribute this is the
    // textbook bad_single_ff_sync shape and CDC-001 would fire.
    (* cdc_sync = "level_2ff" *) logic dst_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
