// Positive fixture: a control bit relayed through three asynchronous
// domains, A → B → C (rtl-buddy-cdc#331).
//
// clk_a, clk_b and clk_c sit in three groups of one
// set_clock_groups -asynchronous, so each hop is a crossing. a_q crosses
// into clk_b through a 2FF synchroniser, clk_b registers the
// synchronised value (gated by a clk_b enable), and b_q crosses into
// clk_c through a second 2FF synchroniser. Two async crossings, both
// with depth 2, so CDC-001 / CDC-002 stay silent; the A → C relation is
// never a crossing of its own because no clk_c flop reads a clk_a flop.
//
//    clk_a          clk_b                         clk_c
//   [ a_q ]──▶[ b_s1 ]─[ b_s2 ]─&─[ b_q ]──▶[ c_s1 ]─[ c_s2 ]──▶ q_out
//   2FF at both hops → clean

module good_three_domain_chain (
    input  logic clk_a,
    input  logic clk_b,
    input  logic clk_c,
    input  logic rst_n,
    input  logic d_in,
    input  logic b_en,
    output logic q_out
);

    logic a_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_in;
    end

    // A → B: 2FF synchroniser, then a clk_b register.
    logic b_s1, b_s2, b_q;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_s1 <= 1'b0;
            b_s2 <= 1'b0;
        end else begin
            b_s1 <= a_q;
            b_s2 <= b_s1;
        end
    end
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= b_s2 & b_en;
    end

    // B → C: 2FF synchroniser.
    logic c_s1, c_s2;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            c_s1 <= 1'b0;
            c_s2 <= 1'b0;
        end else begin
            c_s1 <= b_q;
            c_s2 <= c_s1;
        end
    end

    assign q_out = c_s2;

endmodule
