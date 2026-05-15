// Variant of `marked_user_sync` that tags the destination flop via
// the **output port** declaration instead of an internal `logic`. The
// structural shape (single dst flop, no second stage) still trips
// CDC-001 unless the port-level `(* cdc_sync *)` reaches the netname —
// see issue #38.

module marked_user_sync_port (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    // The user-vetted synchronizer flop's Q output is the module port
    // itself. The attribute lives on the port declaration; the slang
    // frontend used to drop it, masking it from the rule pack.
    (* cdc_sync = "level_2ff" *) output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= src_q;
    end

endmodule
