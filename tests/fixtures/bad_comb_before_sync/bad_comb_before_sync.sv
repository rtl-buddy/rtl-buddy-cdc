// Negative-case fixture: combinational logic between source flops and
// the destination synchronizer's first stage. The two source flops can
// transition independently in src_clk, producing a glitch on the AND
// output that the 2FF synchronizer cannot reliably filter. Should trip
// CDC-003.

module bad_comb_before_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
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

    // BAD: comb logic on the way to the synchronizer first stage.
    logic glitchy;
    assign glitchy = src_q1 & src_q2;

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
