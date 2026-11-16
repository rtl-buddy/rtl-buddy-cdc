// A synchronous reset driven from another clock domain, in the `proc`
// and the `opt_dff` form (rtl-buddy-cdc#383).
//
// `r` is a src_clk register used as the synchronous reset of two dst_clk
// flops: `q`, whose data is a dst_clk input, and `s2`, the second stage
// of a 2FF synchroniser for `s`. After `proc` each reset is a
// constant-leg `$mux` on `D` selected by `r`; after `opt_dff` it is the
// `SRST` pin of an `$sdff`. Both netlists are committed (`.json` and
// `.optdff.json`) and must report the same:
//
//   r -> q    CDC-001  the crossing into q arrives only through its reset
//   r -> s2   CDC-001  likewise for s2 (a second crossing, not s's)
//   r         RDC-003  one finding for the shared foreign reset (q, s2)
//
// `s` -> `s1` -> `s2` is still a 2FF synchroniser: s2's reset only forces
// it to 0, so the chain walk steps over the mux in the `proc` form as it
// reads straight through the `$sdff` in the `opt_dff` form.

module bad_sync_reset_foreign_select (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic r_in,
    input  logic s_in,
    input  logic d_in,
    output logic q,
    output logic s2
);
    logic r, s;
    always_ff @(posedge src_clk) begin
        r <= r_in;
        s <= s_in;
    end

    always_ff @(posedge dst_clk)
        if (r) q <= 1'b0;
        else   q <= d_in;

    logic s1;
    always_ff @(posedge dst_clk) s1 <= s;
    always_ff @(posedge dst_clk)
        if (r) s2 <= 1'b0;
        else   s2 <= s1;
endmodule
