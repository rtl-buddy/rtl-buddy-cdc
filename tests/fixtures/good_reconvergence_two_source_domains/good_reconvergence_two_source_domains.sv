// Positive fixture: signals from two different source domains are each
// synchronised once into a third domain and then combined
// (rtl-buddy-cdc#331).
//
// clk_a, clk_b and clk_c are mutually asynchronous. a_q (clk_a) and b_q
// (clk_b) each cross into clk_c through their own 2FF synchroniser and
// the two synchronised values meet in one clk_c register. That is not
// reconvergence in CDC-005's sense: the rule is about ONE source flop
// whose value is resolved twice, independently, and then recombined.
// a_q and b_q are unrelated launches in unrelated domains, so there is
// no source-side relation for the combine to break, and every crossing
// is a 2FF synchroniser. Two async crossings, no finding.
//
//   clk_a [a_q] ──2FF──┐
//                      ├─ & ─▶ [c_both] (clk_c)
//   clk_b [b_q] ──2FF──┘

module good_reconvergence_two_source_domains (
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

    logic ca_s1, ca_s2, cb_s1, cb_s2, c_both;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            ca_s1 <= 1'b0; ca_s2 <= 1'b0;
            cb_s1 <= 1'b0; cb_s2 <= 1'b0;
        end else begin
            ca_s1 <= a_q;  ca_s2 <= ca_s1;
            cb_s1 <= b_q;  cb_s2 <= cb_s1;
        end
    end
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) c_both <= 1'b0;
        else        c_both <= ca_s2 & cb_s2;
    end

    assign q_out = c_both;

endmodule
