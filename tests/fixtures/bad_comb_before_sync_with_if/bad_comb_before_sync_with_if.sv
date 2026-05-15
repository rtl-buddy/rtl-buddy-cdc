// Negative-case fixture: the bad_comb_before_sync shape but written
// with an always_comb if/else instead of a continuous assign. Both
// source flops fan into the synchronizer first stage through the
// branch-selecting mux, so two CDC-003 violations should fire — one
// per source flop. The fixture exists primarily to drive the slang
// frontend's ConditionalStatement → $mux lowering (issue #36); the
// Yosys frontend also flattens this shape into a $mux post-proc, so
// it's a natural parity target.

module bad_comb_before_sync_with_if (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic sel,
    input  logic a,
    input  logic b,
    output logic q_out
);

    logic src_q1, src_q2;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            src_q1 <= 1'b0;
            src_q2 <= 1'b0;
        end else begin
            src_q1 <= a;
            src_q2 <= b;
        end
    end

    // BAD: branch-selecting comb on the way to the synchronizer.
    logic glitchy;
    always_comb begin
        if (sel)
            glitchy = src_q1;
        else
            glitchy = src_q2;
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
