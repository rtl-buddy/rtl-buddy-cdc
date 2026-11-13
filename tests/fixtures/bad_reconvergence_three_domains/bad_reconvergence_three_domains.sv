// Negative fixture: reconvergence in a third domain, with a second
// source domain feeding the same recombination point
// (rtl-buddy-cdc#331).
//
// clk_a, clk_b and clk_c are mutually asynchronous. a_q (clk_a) fans
// out to TWO independent 2FF synchronisers in clk_c, and b_q (clk_b) to
// one; all three synchronised values meet in one clk_c register. The
// two copies of a_q can resolve a clk_c cycle apart, so the combine can
// see a pair of a_q values that never coexisted: CDC-005 fires exactly
// once, for source a_q into clk_c. b_q has one synchroniser, so it is
// not a reconvergent source although it reaches the same gate, and its
// presence must not add a second CDC-005. Every crossing has depth 2:
// no CDC-001 / CDC-002.
//
//   clk_a [a_q] ──2FF──┐
//               └─2FF──┼─ & | ─▶ [c_mix] (clk_c)   → CDC-005 (a_q)
//   clk_b [b_q] ──2FF──┘

module bad_reconvergence_three_domains (
    input  logic clk_a,
    input  logic clk_b,
    input  logic clk_c,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_b,
    output logic q_out
);

    logic a_q, b_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= d_b;
    end

    // BUG: a_q is synchronised twice into clk_c.
    logic ca0_s1, ca0_s2, ca1_s1, ca1_s2, cb_s1, cb_s2, c_mix;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            ca0_s1 <= 1'b0; ca0_s2 <= 1'b0;
            ca1_s1 <= 1'b0; ca1_s2 <= 1'b0;
            cb_s1  <= 1'b0; cb_s2  <= 1'b0;
        end else begin
            ca0_s1 <= a_q;  ca0_s2 <= ca0_s1;
            ca1_s1 <= a_q;  ca1_s2 <= ca1_s1;
            cb_s1  <= b_q;  cb_s2  <= cb_s1;
        end
    end
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) c_mix <= 1'b0;
        else        c_mix <= (ca0_s2 & ~ca1_s2) | cb_s2;
    end

    assign q_out = c_mix;

endmodule
