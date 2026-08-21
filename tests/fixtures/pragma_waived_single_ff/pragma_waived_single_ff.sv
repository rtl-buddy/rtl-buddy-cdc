// In-RTL pragma fixture: the same single-flop control crossing as
// `bad_single_ff_sync` (CDC-001 fires), waived in place by an
// `// rbcdc: disable-rule` magic comment instead of an external
// waiver file. The run reports the finding as suppressed and exits 0.
//
//        src_clk          dst_clk
//   ┌──[ src_q ]──────[ dst_q ]──── (no further flop on dst_clk)
//   only 1 stage on the destination side → CDC-001, waived by pragma

module pragma_waived_single_ff (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    logic dst_q;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // rbcdc: disable-rule CDC-001 hand-reviewed: q_out is quasi-static
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
