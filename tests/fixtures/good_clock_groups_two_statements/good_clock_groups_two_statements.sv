// Positive fixture: asynchrony declared by two separate
// set_clock_groups statements (rtl-buddy-cdc#331).
//
// The SDC has `-asynchronous -group {clk_a} -group {clk_b}` and,
// separately, `-asynchronous -group {clk_b} -group {clk_c}`. Each
// statement relates only the clocks it names against each other, so
// clk_a / clk_b and clk_b / clk_c are asynchronous, but no statement
// separates clk_a from clk_c: that pair is still timed by STA, and the
// analyzer treats clocks no group separates as synchronous. Hence:
//   clk_a → clk_b   2FF, async
//   clk_b → clk_c   2FF, async
//   clk_a → clk_c   raw single flop, synchronous (related clocks, e.g.
//                   two outputs of one PLL)
// Two async crossings, no finding.
//
//   [a_q] clk_a ──2FF──▶ clk_b [b_q] ──2FF──▶ clk_c
//   [a_q] clk_a ───────raw─────────────────▶ clk_c [c_cap]

module good_clock_groups_two_statements (
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

    // clk_b → clk_c: 2FF synchroniser.
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
    assign q_c = c_s2 ^ c_cap;

endmodule
