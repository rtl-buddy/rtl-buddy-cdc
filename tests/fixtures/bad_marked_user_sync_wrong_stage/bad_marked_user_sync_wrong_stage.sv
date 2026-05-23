// Trust-boundary sentinel (issue #178): `(* cdc_sync *)` placed on
// stage 2 of a 2FF chain instead of stage 1 (the head / first
// destination flop of the crossing).
//
// At the default `--sync-depth 2` this fixture passes trivially —
// the chain is structurally clean and no rule fires either way. To
// expose the trust-scope question we run the rule pack with
// `required_depth=3`: a depth-2 chain is now insufficient and
// CDC-002 should fire on the head (stage 1). The attribute on
// stage 2 is the *wrong flop* — suppression is dst-flop-keyed (the
// crossing's destination is stage 1), so CDC-002 must still fire.
//
// Pins the contract that `(* cdc_sync *)` annotates one specific
// flop and does not retroactively whitelist the entire chain.

module bad_marked_user_sync_wrong_stage (
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

    // Stage 1 — the actual sync first stage, but no attribute here.
    logic stage_1;
    // Stage 2 — wrongly annotated.
    (* cdc_sync = "level_2ff" *) logic stage_2;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            stage_1 <= 1'b0;
            stage_2 <= 1'b0;
        end else begin
            stage_1 <= src_q;
            stage_2 <= stage_1;
        end
    end

    assign q_out = stage_2;

endmodule
