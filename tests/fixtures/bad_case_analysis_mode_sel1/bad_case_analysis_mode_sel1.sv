// Negative fixture: set_case_analysis selects the clock-mux leg that
// makes a raw path a crossing (rtl-buddy-cdc#331).
//
// Same RTL as good_case_analysis_mode_sel0: block M runs on
// clk_m = ~mode[1] ? clk_a : clk_b and captures a_q (clk_a) with a
// single flop, b_q (clk_b) through a 2FF synchroniser, and sends the
// synchronised b_q on to clk_c through a 2FF synchroniser. The SDC pins
// the other mode: `set_case_analysis 1 [get_ports {mode[1]}]`, so clk_m
// is clk_b.
// Now the raw a_q → m_cap path is an asynchronous crossing and CDC-001
// fires exactly once on it; the b_q synchroniser becomes a same-domain
// pipeline (not a crossing) and clk_m → clk_c stays a clean 2FF. m_cap
// feeds only an output port, so the finding is the missing second stage
// (CDC-001), not logic between stages.
//
//   clk_a ─┐                 [a_q] clk_a ──1FF──▶ [m_cap]   → CDC-001
//   clk_b ─┴─ mux(~mode[1]) = clk_m   [b_q] clk_b ──2FF──▶ M   same domain
//                                      M ──2FF──▶ clk_c

module bad_case_analysis_mode_sel1 (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       clk_c,
    input  logic [1:0] mode,
    input  logic       rst_n,
    input  logic       d_a,
    input  logic       d_b,
    output logic       q_m,
    output logic       q_c
);

    // Clock mux: mode[1] = 0 selects clk_a, 1 selects clk_b.
    logic sel_n, clk_m;
    assign sel_n = ~mode[1];
    assign clk_m = sel_n ? clk_a : clk_b;

    logic a_q, b_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= d_b;
    end

    // Block M: a raw capture of a_q, a 2FF synchroniser for b_q.
    logic m_cap, m_s1, m_s2;
    always_ff @(posedge clk_m or negedge rst_n) begin
        if (!rst_n) begin
            m_cap <= 1'b0;
            m_s1  <= 1'b0;
            m_s2  <= 1'b0;
        end else begin
            m_cap <= a_q;
            m_s1  <= b_q;
            m_s2  <= m_s1;
        end
    end
    assign q_m = m_cap ^ m_s2;

    // clk_m → clk_c: 2FF synchroniser.
    logic c_s1, c_s2;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            c_s1 <= 1'b0;
            c_s2 <= 1'b0;
        end else begin
            c_s1 <= m_s2;
            c_s2 <= c_s1;
        end
    end
    assign q_c = c_s2;

endmodule
