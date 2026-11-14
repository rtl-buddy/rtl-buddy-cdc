// Negative fixture: an asynchronous clock captured by one flop in a
// -combinational generated clock domain (rtl-buddy-cdc#331).
//
// Same clocks and SDC as good_gen_clock_edges_invert: ck_div3
// (-edges {1 3 7}), ck_inv (-invert) and ck_gated (-combinational) are
// all generated from clk on internal pins, and clk_x is asynchronous to
// clk through a group that names the root only. The raw chain among the
// clk-derived clocks is still synchronous and silent. clk_x now also
// feeds a single ck_gated flop: ck_gated collapses to clk, which the
// group separates from clk_x, so the crossing is asynchronous and
// CDC-001 fires exactly once (x_q → gx_q). The 2FF synchroniser into clk
// stays clean.
//
//   [a_q] clk ─raw─▶ [d3_q] ck_div3 ─raw─▶ [inv_q] ck_inv
//     ─raw─▶ [g_q] ck_gated ─raw─▶ [back_q] clk          synchronous
//   [x_q] clk_x ──2FF──▶ clk
//   [x_q] clk_x ──1FF──▶ [gx_q] ck_gated                 → CDC-001

module bad_gen_clock_edges_invert (
    input  logic clk,
    input  logic clk_x,
    input  logic rst_n,
    input  logic en_in,
    input  logic d_a,
    input  logic d_x,
    output logic q_gx,
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

    // BUG: clk_x captured by a single ck_gated flop.
    logic gx_q;
    always_ff @(posedge clk_g or negedge rst_n) begin
        if (!rst_n) gx_q <= 1'b0;
        else        gx_q <= x_q;
    end
    assign q_gx = gx_q;

endmodule
