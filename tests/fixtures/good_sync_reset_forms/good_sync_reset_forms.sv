// Synchronous resets in the shapes `opt_dff` folds into SRST
// (rtl-buddy-cdc#394).
//
// After `proc` alone each of these registers is a `$dff` with the reset
// left as constant-leg muxes on D; `opt_dff` and the slang frontend make
// them `$sdff` / `$sdffe` / `$sdffce` with the reset on SRST. The
// reset-domain classification must read every form alike:
//
//   q_hi   `if (r)`                      active high, port `r`
//   q_lo   `if (!rn)`                    active low, port `rn`
//   q_two  `if (r) ... else if (r2)`     two selects
//   q_rov  reset over an enable          `$sdffe`
//   q_eor  reset under an enable         `$sdffce`
//   q_inf  reset from a register         inferred, `r_q`
//   q_one  1-bit, resets to 1            active high, port `r`
//
// One clock, every input typed to it: the design is clean. The
// `.optdff.json` netlist is the same design after `opt_dff`.

module good_sync_reset_forms (
    input  logic       clk,
    input  logic       r,
    input  logic       rn,
    input  logic       r2,
    input  logic       en,
    input  logic [3:0] d,
    output logic [3:0] q_hi,
    output logic [3:0] q_lo,
    output logic [3:0] q_two,
    output logic [3:0] q_rov,
    output logic [3:0] q_eor,
    output logic [3:0] q_inf,
    output logic       q_one
);
    logic r_q;
    always_ff @(posedge clk) r_q <= r2;

    always_ff @(posedge clk)
        if (r) q_hi <= '0;
        else   q_hi <= d;

    always_ff @(posedge clk)
        if (!rn) q_lo <= '0;
        else     q_lo <= d;

    always_ff @(posedge clk)
        if (r)       q_two <= '0;
        else if (r2) q_two <= '0;
        else         q_two <= d;

    always_ff @(posedge clk)
        if (r)       q_rov <= '0;
        else if (en) q_rov <= d;

    always_ff @(posedge clk)
        if (en) begin
            if (r) q_eor <= '0;
            else   q_eor <= d;
        end

    always_ff @(posedge clk)
        if (r_q) q_inf <= '0;
        else     q_inf <= d;

    always_ff @(posedge clk)
        if (r) q_one <= 1'b1;
        else   q_one <= d[0];
endmodule
