// Negative-case fixture: a control crossing with only a single
// destination flop (no second sync stage). Should trip CDC-002.
//
//        src_clk          dst_clk
//   ┌──[ src_q ]──────[ dst_q ]──── (no further flop on dst_clk)
//   only 1 stage on the destination side → CDC-002

module bad_single_ff_sync (
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

    // BAD: single flop on the destination side instead of a 2FF sync.
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
