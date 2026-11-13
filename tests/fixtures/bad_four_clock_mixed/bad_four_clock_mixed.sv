// Negative fixture: four clocks, two synchronous and two asynchronous,
// with two of the five async crossings unsynchronised
// (rtl-buddy-cdc#331).
//
// Same clocks and SDC as good_four_clock_mixed: clk_a2x is a
// -multiply_by 2 generated clock of clk_a, both in the first group of
// one set_clock_groups -asynchronous, clk_b and clk_c in groups of
// their own. Two hops lose their second stage:
//   clk_a2x → clk_b  x_q captured by one flop → CDC-001 (the group
//                    puts clk_a2x itself, not just its master, async)
//   clk_b   → clk_c  b_q captured by one flop → CDC-001
// The raw clk_a → clk_a2x register stays silent (same root, timed by
// STA), and the other three crossings keep their 2FF synchronisers.
// Exactly two CDC-001 and nothing else.
//
//   group 1: clk_a, clk_a2x (×2)   group 2: clk_b   group 3: clk_c
//   [a_q]──raw──▶[x_cap]   x_q ──1FF──▶ clk_b   b_q ──1FF──▶ clk_c

module bad_four_clock_mixed (
    input  logic clk_a,
    input  logic clk_a2x,
    input  logic clk_b,
    input  logic clk_c,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_x,
    input  logic d_b,
    output logic q_x,
    output logic q_b,
    output logic q_c
);

    // One source register per domain.
    logic a_q, x_q, b_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge clk_a2x or negedge rst_n) begin
        if (!rst_n) x_q <= 1'b0;
        else        x_q <= d_x;
    end
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= d_b;
    end

    // clk_a → clk_a2x: same root, timed by STA, a plain register.
    logic x_cap;
    always_ff @(posedge clk_a2x or negedge rst_n) begin
        if (!rst_n) x_cap <= 1'b0;
        else        x_cap <= a_q ^ x_q;
    end
    assign q_x = x_cap;

    // Into clk_b: a_q and x_q, each through a 2FF synchroniser.
    // BUG: x_q (clk_a2x) is captured by a single clk_b flop.
    logic b_a_s1, b_a_s2, b_x_s1;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_a_s1 <= 1'b0; b_a_s2 <= 1'b0;
            b_x_s1 <= 1'b0;
        end else begin
            b_a_s1 <= a_q;  b_a_s2 <= b_a_s1;
            b_x_s1 <= x_q;
        end
    end
    assign q_b = b_a_s2 | b_x_s1;

    // Into clk_c: a_q and x_q through 2FF synchronisers; BUG: b_q is
    // captured by a single clk_c flop.
    logic c_a_s1, c_a_s2, c_x_s1, c_x_s2, c_b_s1;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            c_a_s1 <= 1'b0; c_a_s2 <= 1'b0;
            c_x_s1 <= 1'b0; c_x_s2 <= 1'b0;
            c_b_s1 <= 1'b0;
        end else begin
            c_a_s1 <= a_q;  c_a_s2 <= c_a_s1;
            c_x_s1 <= x_q;  c_x_s2 <= c_x_s1;
            c_b_s1 <= b_q;
        end
    end
    assign q_c = c_a_s2 ^ c_x_s2 ^ c_b_s1;

endmodule
