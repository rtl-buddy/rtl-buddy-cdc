// Negative fixture: asynchrony declared by two separate
// set_clock_groups statements, with one async hop unsynchronised
// (rtl-buddy-cdc#331).
//
// Same SDC as good_clock_groups_two_statements: clk_a / clk_b are
// asynchronous by one statement, clk_b / clk_c by another, and no
// statement separates clk_a from clk_c. The clk_b → clk_c hop is now a
// single capture flop: the second statement makes that pair
// asynchronous, so CDC-001 fires exactly once (b_q → c_s1). The raw
// clk_a → clk_c capture stays silent (the two clocks are never declared
// asynchronous) and clk_a → clk_b keeps its 2FF synchroniser.
//
//   [a_q] clk_a ──2FF──▶ clk_b [b_q] ──1FF──▶ clk_c   → CDC-001
//   [a_q] clk_a ───────raw─────────────────▶ clk_c [c_cap]

module bad_clock_groups_two_statements (
    input  logic clk_a,
    input  logic clk_b,
    input  logic clk_c,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_b,
    output logic q_b,
    output logic q_c
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

    // clk_a → clk_b: 2FF synchroniser.
    logic b_s1, b_s2;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_s1 <= 1'b0;
            b_s2 <= 1'b0;
        end else begin
            b_s1 <= a_q;
            b_s2 <= b_s1;
        end
    end
    assign q_b = b_s2;

    // clk_a → clk_c: related clocks, a plain register.
    logic c_cap;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) c_cap <= 1'b0;
        else        c_cap <= a_q;
    end

    // clk_b → clk_c: BUG, a single capture flop.
    logic c_s1;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) c_s1 <= 1'b0;
        else        c_s1 <= b_q;
    end
    assign q_c = c_s1 ^ c_cap;

endmodule
