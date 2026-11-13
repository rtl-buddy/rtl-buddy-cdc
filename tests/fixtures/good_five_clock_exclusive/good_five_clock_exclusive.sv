// Positive fixture: five clocks mixing asynchronous and exclusive
// groups, so some apparent crossings are unreachable (rtl-buddy-cdc#331).
//
// One set_clock_groups -asynchronous puts all five clocks in groups of
// their own. Two more statements declare exclusive pairs:
// clk_m0 / clk_m1 -physically_exclusive (two modes, only one clock is
// ever running) and clk_io / clk_dbg -logically_exclusive (functional
// vs debug mode). An apparent crossing between the two members of one
// exclusive statement is unreachable and is dropped before any rule
// runs, so its raw single-flop capture is not a finding:
//   clk_m0 → clk_m1   raw  unreachable (physically exclusive)
//   clk_io → clk_dbg  raw  unreachable (logically exclusive)
// Every other crossing is reachable and asynchronous, and lands in a 2FF
// synchroniser: m0 / m1 / io / dbg → clk_sys, and clk_m1 → clk_dbg (the
// two exclusive statements do not relate clk_m1 to clk_dbg). Five async
// crossings, no finding.
//
//   clk_m0 ─raw─▶ clk_m1   (excl)      clk_io ─raw─▶ clk_dbg   (excl)
//   m0, m1, io, dbg ──2FF──▶ clk_sys    m1 ──2FF──▶ clk_dbg

module good_five_clock_exclusive (
    input  logic clk_sys,
    input  logic clk_m0,
    input  logic clk_m1,
    input  logic clk_io,
    input  logic clk_dbg,
    input  logic rst_n,
    input  logic d_m0,
    input  logic d_m1,
    input  logic d_io,
    input  logic d_dbg,
    output logic q_sys,
    output logic q_m1,
    output logic q_dbg
);

    // One source register per non-sys domain.
    logic m0_q, m1_q, io_q, dbg_q;
    always_ff @(posedge clk_m0 or negedge rst_n) begin
        if (!rst_n) m0_q <= 1'b0;
        else        m0_q <= d_m0;
    end
    always_ff @(posedge clk_m1 or negedge rst_n) begin
        if (!rst_n) m1_q <= 1'b0;
        else        m1_q <= d_m1;
    end
    always_ff @(posedge clk_io or negedge rst_n) begin
        if (!rst_n) io_q <= 1'b0;
        else        io_q <= d_io;
    end
    always_ff @(posedge clk_dbg or negedge rst_n) begin
        if (!rst_n) dbg_q <= 1'b0;
        else        dbg_q <= d_dbg;
    end

    // clk_m0 → clk_m1: raw capture, but the clocks never run together.
    logic m1_cap;
    always_ff @(posedge clk_m1 or negedge rst_n) begin
        if (!rst_n) m1_cap <= 1'b0;
        else        m1_cap <= m0_q;
    end
    assign q_m1 = m1_cap ^ m1_q;

    // clk_io → clk_dbg: raw capture, but the modes are exclusive.
    logic dbg_cap;
    always_ff @(posedge clk_dbg or negedge rst_n) begin
        if (!rst_n) dbg_cap <= 1'b0;
        else        dbg_cap <= io_q;
    end

    logic dbg_m1_s1, dbg_m1_s2;
    always_ff @(posedge clk_dbg or negedge rst_n) begin
        if (!rst_n) begin
            dbg_m1_s1 <= 1'b0;   dbg_m1_s2 <= 1'b0;
        end else begin
            dbg_m1_s1 <= m1_q;   dbg_m1_s2 <= dbg_m1_s1;
        end
    end
    assign q_dbg = dbg_cap ^ dbg_q ^ dbg_m1_s2;

    // Into clk_sys: one synchroniser per source domain.
    logic sys_m0_s1, sys_m0_s2, sys_m1_s1, sys_m1_s2;
    logic sys_dbg_s1, sys_dbg_s2, sys_io_s1, sys_io_s2;
    always_ff @(posedge clk_sys or negedge rst_n) begin
        if (!rst_n) begin
            sys_m0_s1 <= 1'b0;   sys_m0_s2 <= 1'b0;
            sys_m1_s1 <= 1'b0;   sys_m1_s2 <= 1'b0;
            sys_dbg_s1 <= 1'b0;  sys_dbg_s2 <= 1'b0;
            sys_io_s1 <= 1'b0;   sys_io_s2 <= 1'b0;
        end else begin
            sys_m0_s1 <= m0_q;   sys_m0_s2 <= sys_m0_s1;
            sys_m1_s1 <= m1_q;   sys_m1_s2 <= sys_m1_s1;
            sys_dbg_s1 <= dbg_q; sys_dbg_s2 <= sys_dbg_s1;
            sys_io_s1 <= io_q;   sys_io_s2 <= sys_io_s1;
        end
    end
    assign q_sys = sys_m0_s2 ^ sys_m1_s2 ^ sys_dbg_s2 ^ sys_io_s2;

endmodule
