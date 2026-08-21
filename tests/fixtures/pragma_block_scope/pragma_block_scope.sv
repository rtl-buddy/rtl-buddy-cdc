// Block-scoped in-RTL pragma fixture: two identical single-flop
// control crossings src_clk -> dst_clk, both of which trip CDC-001.
// The first sits inside a disable-rule / enable-rule pragma block for
// CDC-001 and is suppressed; the second sits after the block closes
// and still fires. The pair is the regression net for the half-open
// [disable, enable) range. (The pragma text itself is only written
// below, in the code — a prose copy of it up here would open a second
// region, since the scanner reads every comment line.)
//
//        src_clk               dst_clk
//   ┌──[ src_a ]──────────[ dst_a ]──  inside the block  → waived
//   └──[ src_b ]──────────[ dst_b ]──  outside the block → CDC-001

module pragma_block_scope (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic a_in,
    input  logic b_in,
    output logic a_out,
    output logic b_out
);

    logic src_a, src_b;
    logic dst_a, dst_b;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            src_a <= 1'b0;
            src_b <= 1'b0;
        end else begin
            src_a <= a_in;
            src_b <= b_in;
        end
    end

    // rbcdc: disable-rule CDC-001 a_out is quasi-static, hand-reviewed
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_a <= 1'b0;
        else        dst_a <= src_a;
    end
    // rbcdc: enable-rule CDC-001

    // Outside the block: the same shape, still a violation.
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_b <= 1'b0;
        else        dst_b <= src_b;
    end

    assign a_out = dst_a;
    assign b_out = dst_b;

endmodule
