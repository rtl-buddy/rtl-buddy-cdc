// Negative-case fixture: the bad_comb_before_sync shape but written
// with an always_comb case instead of a continuous assign. Multiple
// source flops fan into the synchronizer first stage through a
// case-mux chain, so CDC-003 should fire — one violation per source
// flop that reaches the sync cone. Drives the slang frontend's
// CaseStatement → chained-$mux lowering (issue #37).

module bad_comb_case_before_sync (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       rst_n,
    input  logic [1:0] sel,
    input  logic       a,
    input  logic       b,
    input  logic       c,
    output logic       q_out
);

    logic src_q1, src_q2, src_q3;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            src_q1 <= 1'b0;
            src_q2 <= 1'b0;
            src_q3 <= 1'b0;
        end else begin
            src_q1 <= a;
            src_q2 <= b;
            src_q3 <= c;
        end
    end

    // BAD: case-mux on the way to the synchronizer first stage.
    logic glitchy;
    always_comb begin
        case (sel)
            2'b00:   glitchy = src_q1;
            2'b01:   glitchy = src_q2;
            default: glitchy = src_q3;
        endcase
    end

    logic sync_stage0, sync_stage1;

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            sync_stage0 <= 1'b0;
            sync_stage1 <= 1'b0;
        end else begin
            sync_stage0 <= glitchy;
            sync_stage1 <= sync_stage0;
        end
    end

    assign q_out = sync_stage1;

endmodule
