// Trust-boundary sentinel (issue #178): `(* cdc_sync *)` placed on
// a STRAY flop that isn't on the crossing path. Same dst_clk domain,
// but functionally unrelated to the cross-domain signal — the user
// has mis-labelled a regular data register hoping for some effect.
//
// The marked flop suppresses nothing (it isn't the dst flop of any
// async crossing); CDC-001 must still fire on the actual 1FF
// "chain" elsewhere in the design.

module bad_marked_user_sync_stray_flop (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    input  logic decoy_in,
    output logic q_out,
    output logic decoy_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // The actually-broken sync: depth-1 chain. CDC-001 fires here.
    logic dst_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    // Stray flop in dst_clk's domain, fed by a same-domain input.
    // The user mis-marks this one. The attribute lands on `decoy_q`,
    // which is not the dst flop of any async crossing — suppression
    // is dst-flop-keyed, so this is a no-op.
    (* cdc_sync = "level_2ff" *) logic decoy_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) decoy_q <= 1'b0;
        else        decoy_q <= decoy_in;
    end

    assign q_out     = dst_q;
    assign decoy_out = decoy_q;

endmodule
