// Reset polarity of set / clear flops (rtl-buddy-cdc#394).
//
// `proc` writes a set / clear flop as a `$dffsr` with active-high CLR and
// SET pins, so an active-low clear becomes an inverter mux in front of
// CLR. RDC-002 compares the polarity the reset signal acts with, not the
// pin's: `rst_n` is declared active low and every clear below that is
// written `negedge` must be clean.
//
//   q_ok    clear from `rst_n` (negedge)                    clean
//   q_ok_f  clear from `r_lo`, a flop that resets to 0      clean
//   q_bad   clear from `rst_n`, but `posedge`               RDC-002 (port)
//   q_bad_f clear from `r_lo`, but `posedge`                RDC-002 (flop)
//   s1, s2  a reset synchroniser of `arst_n`, built from set /
//           clear flops; its head loads 1, the deassertion
//           value of the active-low clear; q_sy takes its
//           output                                       clean (RDC-007)
//
// The committed `.synth.json` holds the gate-level form (`$_DFFSR_*`)
// and must agree.

module bad_set_clear_reset_polarity (
    input  logic clk,
    (* reset_polarity = "low" *)
    input  logic rst_n,
    input  logic set_n,
    input  logic arst_n,
    input  logic d_in,
    output logic q_ok,
    output logic q_ok_f,
    output logic q_bad,
    output logic q_bad_f,
    output logic q_sy
);
    // An ordinary register that loads 0 while `rst_n` is asserted; its
    // output is the clear of q_ok_f / q_bad_f.
    logic r_lo;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) r_lo <= 1'b0;
        else        r_lo <= d_in;

    always_ff @(posedge clk or negedge rst_n or negedge set_n)
        if (!rst_n)      q_ok <= 1'b0;
        else if (!set_n) q_ok <= 1'b1;
        else             q_ok <= d_in;

    always_ff @(posedge clk or negedge r_lo or negedge set_n)
        if (!r_lo)       q_ok_f <= 1'b0;
        else if (!set_n) q_ok_f <= 1'b1;
        else             q_ok_f <= d_in;

    always_ff @(posedge clk or posedge rst_n or negedge set_n)
        if (rst_n)       q_bad <= 1'b0;
        else if (!set_n) q_bad <= 1'b1;
        else             q_bad <= d_in;

    always_ff @(posedge clk or posedge r_lo or negedge set_n)
        if (r_lo)        q_bad_f <= 1'b0;
        else if (!set_n) q_bad_f <= 1'b1;
        else             q_bad_f <= d_in;

    logic s1, s2;
    always_ff @(posedge clk or negedge arst_n or negedge set_n)
        if (!arst_n)     begin s1 <= 1'b0; s2 <= 1'b0; end
        else if (!set_n) begin s1 <= 1'b1; s2 <= 1'b1; end
        else             begin s1 <= 1'b1; s2 <= s1;   end

    always_ff @(posedge clk or negedge s2)
        if (!s2) q_sy <= 1'b0;
        else     q_sy <= d_in;

endmodule
