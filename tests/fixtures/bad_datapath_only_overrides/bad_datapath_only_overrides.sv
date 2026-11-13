// Negative fixture: set_max_delay -datapath_only overriding a generated
// clock relation and an exclusive group, with raw captures
// (rtl-buddy-cdc#331).
//
// Same SDC as good_datapath_only_overrides: clk_half is a -divide_by 2
// generated clock of clk, ck_m0 / ck_m1 are -physically_exclusive, and
// each pair carries a clock-to-clock set_max_delay -datapath_only. The
// datapath-only bound makes each pair an intended asynchronous crossing
// (it wins over the generated collapse and is never unreachable), so a
// single capture flop is a missing synchroniser in both places. Without
// the bound, the first raw capture would be a synchronous path and the
// second an unreachable one, both silent. Exactly two CDC-001.
//
//   [a_q] clk   ──1FF──▶ clk_half   → CDC-001
//   [m0_q] ck_m0 ──1FF──▶ ck_m1      → CDC-001

module bad_datapath_only_overrides (
    input  logic clk,
    input  logic clk_half,
    input  logic ck_m0,
    input  logic ck_m1,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_m0,
    output logic q_half,
    output logic q_m1
);

    logic a_q, m0_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge ck_m0 or negedge rst_n) begin
        if (!rst_n) m0_q <= 1'b0;
        else        m0_q <= d_m0;
    end

    // clk → clk_half: BUG, a single capture flop.
    logic h_s1;
    always_ff @(posedge clk_half or negedge rst_n) begin
        if (!rst_n) h_s1 <= 1'b0;
        else        h_s1 <= a_q;
    end
    assign q_half = h_s1;

    // ck_m0 → ck_m1: BUG, a single capture flop.
    logic m1_s1;
    always_ff @(posedge ck_m1 or negedge rst_n) begin
        if (!rst_n) m1_s1 <= 1'b0;
        else        m1_s1 <= m0_q;
    end
    assign q_m1 = m1_s1;

endmodule
