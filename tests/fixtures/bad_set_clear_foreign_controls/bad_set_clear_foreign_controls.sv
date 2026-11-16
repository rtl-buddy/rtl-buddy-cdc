// Set / clear flops whose async controls are wrong (rtl-buddy-cdc#383,
// rtl-buddy-cdc#378).
//
// `proc` writes a set / clear flop as a `$dffsr` whose CLR and SET are
// built from muxes: an active-low control becomes an inverter mux, and
// the lower-priority pin is gated by the higher one (`SET = rst_n ?
// ~set_n : 0`). That encoding is not reset logic; what drives it is:
//
//   qa  SET from `set_q`, a src_clk register          RDC-001
//   qb  SET from `sel ? set_a_n : set_b_n` (a mux)    RDC-006
//   qc  CLR from `clr_q`, a src_clk register          RDC-001
//
// qa and qc share no source, so RDC-001 fires twice. qc's CLR is the
// buffer mux `clr_q ? 1 : 0`: a register, not combinational logic, so no
// RDC-004. Every flop's clear or set from the ports `rst_n` / `set_n`
// is clean. The committed `.synth.json` (`opt_dff; techmap; abc`) holds
// the gate-level form (`$_DFFSR_*`, `$_NOT_` / `$_AND_` on R and S) and
// must agree.

module bad_set_clear_foreign_controls (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic set_n,
    input  logic set_in,
    input  logic clr_in,
    input  logic sel,
    input  logic set_a_n,
    input  logic set_b_n,
    input  logic d_in,
    output logic qa,
    output logic qb,
    output logic qc
);
    logic set_q, clr_q;
    always_ff @(posedge src_clk) begin
        set_q <= set_in;
        clr_q <= clr_in;
    end

    always_ff @(posedge dst_clk or negedge rst_n or negedge set_q)
        if (!rst_n)      qa <= 1'b0;
        else if (!set_q) qa <= 1'b1;
        else             qa <= d_in;

    wire set_mux_n = sel ? set_a_n : set_b_n;
    always_ff @(posedge dst_clk or negedge rst_n or negedge set_mux_n)
        if (!rst_n)          qb <= 1'b0;
        else if (!set_mux_n) qb <= 1'b1;
        else                 qb <= d_in;

    always_ff @(posedge dst_clk or posedge clr_q or negedge set_n)
        if (clr_q)       qc <= 1'b0;
        else if (!set_n) qc <= 1'b1;
        else             qc <= d_in;
endmodule
