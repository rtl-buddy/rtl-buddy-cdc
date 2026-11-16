// One foreign control bit fanned out to every lane of a register
// (rtl-buddy-cdc#390).
//
//   q_r  4-bit, sync reset from `r`, a src_clk register    CDC-001, RDC-003
//   q_e  4-bit, enable from `en`, a src_clk register       CDC-001
//   q_c  4-bit, enable from `ctl[0]`, one bit of a 2-bit   CDC-001
//        src_clk register
//
// Every lane sees the same bit, so there is no inter-bit skew to be
// incoherent: each is a single-bit crossing that a 2FF synchroniser on
// the control fixes, not a bus needing gray code or a handshake. They
// were W-bit bus crossings (CDC-004), because the walk reaches every
// `D` lane through the reset / enable mux select (or the `SRST` / `EN`
// pin it is folded into). The committed `.optdff.json` (`$sdff` /
// `$dffe`) and `.synth.json` (gate level) must agree.

module bad_control_bit_fanout (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       r_in,
    input  logic       en_in,
    input  logic [1:0] ctl_in,
    input  logic [3:0] d,
    output logic [3:0] q_r,
    output logic [3:0] q_e,
    output logic [3:0] q_c,
    output logic       ctl_hi
);
    logic       r, en;
    logic [1:0] ctl;
    always_ff @(posedge src_clk) begin
        r   <= r_in;
        en  <= en_in;
        ctl <= ctl_in;
    end
    assign ctl_hi = ctl[1];

    always_ff @(posedge dst_clk)
        if (r) q_r <= 4'd0;
        else   q_r <= d;

    always_ff @(posedge dst_clk)
        if (en) q_e <= d;

    always_ff @(posedge dst_clk)
        if (ctl[0]) q_c <= d;
endmodule
