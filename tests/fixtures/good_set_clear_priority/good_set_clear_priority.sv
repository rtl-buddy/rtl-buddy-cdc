// Set / clear flops on reset ports, in every priority and polarity
// (rtl-buddy-cdc#383, rtl-buddy-cdc#378).
//
// `proc` writes each as a `$dffsr` whose CLR and SET are muxes: the
// lower-priority pin is gated by the higher one (`SET = rst_n ? ~set_n :
// 0`, or `CLR = set_n ? ~rst_n : 0` when set wins) and an active-low
// control is an inverter mux. That is how Yosys encodes the flop, not a
// reset mux or two resets converging, so RDC-005 / RDC-006 must not
// fire on either pin. The data path is a plain dst_clk register. The
// committed `.synth.json` (gate level: `$_NOT_` / `$_AND_` on R and S)
// must agree: no finding.

module good_set_clear_priority (
    input  logic dst_clk,
    input  logic rst_n,
    input  logic set_n,
    input  logic rst,
    input  logic set,
    input  logic d_in,
    output logic q_clr_first,
    output logic q_set_first,
    output logic q_high
);
    always_ff @(posedge dst_clk or negedge rst_n or negedge set_n)
        if (!rst_n)      q_clr_first <= 1'b0;
        else if (!set_n) q_clr_first <= 1'b1;
        else             q_clr_first <= d_in;

    always_ff @(posedge dst_clk or negedge rst_n or negedge set_n)
        if (!set_n)      q_set_first <= 1'b1;
        else if (!rst_n) q_set_first <= 1'b0;
        else             q_set_first <= d_in;

    always_ff @(posedge dst_clk or posedge rst or posedge set)
        if (rst)      q_high <= 1'b0;
        else if (set) q_high <= 1'b1;
        else          q_high <= d_in;
endmodule
