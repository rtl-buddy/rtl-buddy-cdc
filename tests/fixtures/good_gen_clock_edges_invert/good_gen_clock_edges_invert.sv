// Positive fixture: generated clocks declared with -edges, -invert and
// -combinational (rtl-buddy-cdc#331).
//
// Three clocks are derived from clk inside the design and declared on
// internal pins:
//   ck_div3   -edges {1 3 7}: a divide-by-3 counter's registered output
//             (high one clk cycle in three)
//   ck_inv    -invert -divide_by 1: clk through an inverter
//   ck_gated  -combinational: clk ANDed with a falling-edge enable flop
// Each has -master_clock clk, so all three collapse to the root clk and
// every path among clk, ck_div3, ck_inv and ck_gated is synchronous: the
// raw single-flop chain clk → ck_div3 → ck_inv → ck_gated → clk is
// timed by STA, not a CDC crossing. clk_x is asynchronous to clk (the
// group names the root) and enters clk through a 2FF synchroniser. One
// async crossing, no finding.
//
//   [a_q] clk ─raw─▶ [d3_q] ck_div3 ─raw─▶ [inv_q] ck_inv
//     ─raw─▶ [g_q] ck_gated ─raw─▶ [back_q] clk          synchronous
//   [x_q] clk_x ──2FF──▶ clk

module good_gen_clock_edges_invert (
    input  logic clk,
    input  logic clk_x,
    input  logic rst_n,
    input  logic en_in,
    input  logic d_a,
    input  logic d_x,
    output logic q_back,
    output logic q_x
);

    // ck_div3: registered terminal count of a divide-by-3 counter.
    logic [1:0] cnt;
    logic       div3_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt    <= 2'd0;
            div3_q <= 1'b0;
        end else begin
            cnt    <= (cnt == 2'd2) ? 2'd0 : cnt + 2'd1;
            div3_q <= (cnt == 2'd2);
        end
    end

    // ck_inv: inverted clk.
    logic clk_n;
    assign clk_n = ~clk;

    // ck_gated: clk gated by an enable registered on the falling edge.
    logic en_l, clk_g;
    always_ff @(negedge clk or negedge rst_n) begin
        if (!rst_n) en_l <= 1'b0;
        else        en_l <= en_in;
    end
    assign clk_g = clk & en_l;

    // Raw single-flop chain through the clk-derived clocks.
    logic a_q, d3_q, inv_q, g_q, back_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge div3_q or negedge rst_n) begin
        if (!rst_n) d3_q <= 1'b0;
        else        d3_q <= a_q;
    end
    always_ff @(posedge clk_n or negedge rst_n) begin
        if (!rst_n) inv_q <= 1'b0;
        else        inv_q <= d3_q;
    end
    always_ff @(posedge clk_g or negedge rst_n) begin
        if (!rst_n) g_q <= 1'b0;
        else        g_q <= inv_q;
    end
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) back_q <= 1'b0;
        else        back_q <= g_q;
    end
    assign q_back = back_q;

    // clk_x → clk: 2FF synchroniser.
    logic x_q, x_s1, x_s2;
    always_ff @(posedge clk_x or negedge rst_n) begin
        if (!rst_n) x_q <= 1'b0;
        else        x_q <= d_x;
    end
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_s1 <= 1'b0;
            x_s2 <= 1'b0;
        end else begin
            x_s1 <= x_q;
            x_s2 <= x_s1;
        end
    end
    assign q_x = x_s2;

endmodule
