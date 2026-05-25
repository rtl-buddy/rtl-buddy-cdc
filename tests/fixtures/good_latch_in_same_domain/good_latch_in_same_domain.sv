// Positive counterpart for CDC-017.
//
// A transparent latch is fine when source and destination flops
// share a clock domain — no CDC at all. CDC-017 must stay silent
// here even though structurally the latch sits between two flops:
// the rule only fires when src_clock != dst_clock.

module good_latch_in_same_domain (
    input  logic clk,
    input  logic latch_en,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge clk) src_q <= d_in;

    logic latch_q;
    always_latch
        if (latch_en) latch_q = src_q;

    logic dst_q;
    always_ff @(posedge clk) dst_q <= latch_q;

    assign q_out = dst_q;

endmodule
