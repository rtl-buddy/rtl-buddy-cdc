// Negative fixture: a three-domain relay A → B → C with the second hop
// unsynchronised (rtl-buddy-cdc#331).
//
// Same clocks and SDC as good_three_domain_chain: clk_a, clk_b and
// clk_c are three groups of one set_clock_groups -asynchronous. The
// A → B hop is a correct 2FF synchroniser; the B → C hop is captured by
// a single clk_c flop that drives the output directly. CDC-001 must
// fire exactly once, on the B → C crossing (b_q → c_s1), and nothing
// on the synchronised A → B hop.
//
//    clk_a          clk_b                         clk_c
//   [ a_q ]──▶[ b_s1 ]─[ b_s2 ]─&─[ b_q ]──▶[ c_s1 ]──▶ q_out
//   2FF at A → B; single flop at B → C → CDC-001

module bad_three_domain_chain (
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

    // B → C: BUG — one capture flop, no second stage.
    logic c_s1;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) c_s1 <= 1'b0;
        else        c_s1 <= b_q;
    end

    assign q_out = c_s1;

endmodule
