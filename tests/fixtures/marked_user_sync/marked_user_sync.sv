// Mark a flop with `(* cdc_sync *)` to declare it a user-vetted
// synchronizer first stage. The structural shape here (single dst
// flop, no second stage) would normally trip CDC-001, but the
// attribute tells the analyzer the user has taken responsibility:
// the rule should stand down.

module marked_user_sync (
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

    // (* cdc_sync *) marks this as a user-declared synchronizer.
    // CDC-001/-002/-003 will skip it.
    (* cdc_sync = "level_2ff" *) logic dst_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
