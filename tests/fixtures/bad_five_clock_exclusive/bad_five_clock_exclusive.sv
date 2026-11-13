// Negative fixture: five clocks mixing asynchronous and exclusive
// groups, with two reachable crossings left unsynchronised
// (rtl-buddy-cdc#331).
//
// Same clocks and SDC as good_five_clock_exclusive: all five clocks are
// mutually asynchronous, clk_m0 / clk_m1 are -physically_exclusive and
// clk_io / clk_dbg -logically_exclusive. The two raw captures between
// members of one exclusive statement (clk_m0 → clk_m1,
// clk_io → clk_dbg) stay unreachable and silent. Two reachable
// crossings lose their synchroniser:
//   clk_io → clk_sys   io_q captured by one flop → CDC-001
//   clk_m1 → clk_dbg   m1_q captured by one flop → CDC-001: clk_m1 and
//                      clk_dbg sit in different exclusive statements,
//                      which never relate them, so the pair is live
// Exactly two CDC-001 and nothing else.
//
//   clk_m0 ─raw─▶ clk_m1   (excl)      clk_io ─raw─▶ clk_dbg   (excl)
//   io ──1FF──▶ clk_sys                 m1 ──1FF──▶ clk_dbg

module bad_five_clock_exclusive (
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

    // BUG: m1_q is captured by a single clk_dbg flop.
    logic dbg_m1_s1;
    always_ff @(posedge clk_dbg or negedge rst_n) begin
        if (!rst_n) begin
            dbg_m1_s1 <= 1'b0;
        end else begin
            dbg_m1_s1 <= m1_q;
        end
    end
    assign q_dbg = dbg_cap ^ dbg_q ^ dbg_m1_s1;

    // Into clk_sys: one synchroniser per source domain.
    // BUG: io_q is captured by a single clk_sys flop.
    logic sys_m0_s1, sys_m0_s2, sys_m1_s1, sys_m1_s2;
    logic sys_dbg_s1, sys_dbg_s2, sys_io_s1;
    always_ff @(posedge clk_sys or negedge rst_n) begin
        if (!rst_n) begin
            sys_m0_s1 <= 1'b0;   sys_m0_s2 <= 1'b0;
            sys_m1_s1 <= 1'b0;   sys_m1_s2 <= 1'b0;
            sys_dbg_s1 <= 1'b0;  sys_dbg_s2 <= 1'b0;
            sys_io_s1 <= 1'b0;
        end else begin
            sys_m0_s1 <= m0_q;   sys_m0_s2 <= sys_m0_s1;
            sys_m1_s1 <= m1_q;   sys_m1_s2 <= sys_m1_s1;
            sys_dbg_s1 <= dbg_q; sys_dbg_s2 <= sys_dbg_s1;
            sys_io_s1 <= io_q;
        end
    end
    assign q_sys = sys_m0_s2 ^ sys_m1_s2 ^ sys_dbg_s2 ^ sys_io_s1;

endmodule
