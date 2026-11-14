// Positive fixture: set_max_delay -datapath_only overriding a generated
// clock relation and an exclusive group (rtl-buddy-cdc#331).
//
// clk_half is a create_generated_clock -divide_by 2 of clk (brought in
// on its own port), and ck_m0 / ck_m1 are -physically_exclusive. On
// their own, the first pair would be synchronous (same root) and the
// second unreachable. Each pair also carries a clock-to-clock
// `set_max_delay -datapath_only`, which bounds only the datapath of a
// path whose clocks are not timed against each other and which its
// author believes is live. A datapath-only pair wins over both the
// generated collapse and the exclusive group, so both crossings are
// checked as asynchronous; both land in a 2FF synchroniser. Two async
// crossings, no finding.
//
//   [a_q] clk   ──2FF──▶ clk_half   (generated ÷2, datapath-only)
//   [m0_q] ck_m0 ──2FF──▶ ck_m1      (exclusive, datapath-only)

module good_datapath_only_overrides (
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

    // clk → clk_half: 2FF synchroniser.
    logic h_s1, h_s2;
    always_ff @(posedge clk_half or negedge rst_n) begin
        if (!rst_n) begin
            h_s1 <= 1'b0;
            h_s2 <= 1'b0;
        end else begin
            h_s1 <= a_q;
            h_s2 <= h_s1;
        end
    end
    assign q_half = h_s2;

    // ck_m0 → ck_m1: 2FF synchroniser.
    logic m1_s1, m1_s2;
    always_ff @(posedge ck_m1 or negedge rst_n) begin
        if (!rst_n) begin
            m1_s1 <= 1'b0;
            m1_s2 <= 1'b0;
        end else begin
            m1_s1 <= m0_q;
            m1_s2 <= m1_s1;
        end
    end
    assign q_m1 = m1_s2;

endmodule
