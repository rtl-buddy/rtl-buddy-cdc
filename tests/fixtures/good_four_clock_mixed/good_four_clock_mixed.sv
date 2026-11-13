// Positive fixture: four clocks, two synchronous and two asynchronous,
// with a crossing for every clock pair (rtl-buddy-cdc#331).
//
// clk_a2x is a create_generated_clock -multiply_by 2 of clk_a (a PLL
// output brought in on its own port), so clk_a and clk_a2x share a
// root. One set_clock_groups -asynchronous puts both in the first
// group and clk_b, clk_c in groups of their own. Six pairs:
//   clk_a  → clk_a2x  same root, raw single flop: synchronous, no check
//   clk_a  → clk_b    2FF        clk_a  → clk_c    2FF
//   clk_a2x→ clk_b    2FF        clk_a2x→ clk_c    2FF
//   clk_b  → clk_c    2FF
// Five async crossings, each into a 2FF synchroniser: clean.
//
//   group 1: clk_a, clk_a2x (×2)   group 2: clk_b   group 3: clk_c
//   [a_q]──raw──▶[x_cap]            [b_*_s2]          [c_*_s2]
//   a_q, x_q ──2FF──▶ clk_b   a_q, x_q, b_q ──2FF──▶ clk_c

module good_four_clock_mixed (
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
    logic b_a_s1, b_a_s2, b_x_s1, b_x_s2;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_a_s1 <= 1'b0; b_a_s2 <= 1'b0;
            b_x_s1 <= 1'b0; b_x_s2 <= 1'b0;
        end else begin
            b_a_s1 <= a_q;  b_a_s2 <= b_a_s1;
            b_x_s1 <= x_q;  b_x_s2 <= b_x_s1;
        end
    end
    assign q_b = b_a_s2 | b_x_s2;

    // Into clk_c: a_q, x_q and b_q, each through a 2FF synchroniser.
    logic c_a_s1, c_a_s2, c_x_s1, c_x_s2, c_b_s1, c_b_s2;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            c_a_s1 <= 1'b0; c_a_s2 <= 1'b0;
            c_x_s1 <= 1'b0; c_x_s2 <= 1'b0;
            c_b_s1 <= 1'b0; c_b_s2 <= 1'b0;
        end else begin
            c_a_s1 <= a_q;  c_a_s2 <= c_a_s1;
            c_x_s1 <= x_q;  c_x_s2 <= c_x_s1;
            c_b_s1 <= b_q;  c_b_s2 <= c_b_s1;
        end
    end
    assign q_c = c_a_s2 ^ c_x_s2 ^ c_b_s2;

endmodule
