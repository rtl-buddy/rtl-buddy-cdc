// CDC-010 negative case — a clock mux whose select is driven by a
// flop in a foreign clock domain.
//
// Without synchronization the select's transition (on a ck1 edge)
// chops the muxed output clock mid-cycle, producing a sub-period
// runt pulse that the downstream flop will sample as a real edge.
// Even with both mux inputs in the ck0 domain — same upstream
// source, only differing by which leg the user picked — the
// asynchronous select is unsafe: the analyzer fires structurally
// rather than try to prove input equivalence at the AC level.
//
// Both ck0_a / ck0_b are declared as the same clock (`ck0`) in the
// SDC so the cell's clock-input-domain set collapses to `{ck0}`.
// `sel_q` sits in `ck1`, which is in a different async group than
// `ck0` — CDC-010 fires once on the `$mux`'s S pin.

module bad_async_clock_mux (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);

    // Select is in ck1's domain — the foreign-domain source.
    logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    // "Clock mux" — both inputs are legitimate clocks (both ck0),
    // but the select is from ck1. The structural shape — flop-Q on
    // a `$mux.S` whose A / B trace back to a different clock domain
    // — is what the rule detects.
    logic ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    // Downstream flop driven by the muxed clock — every flop on
    // this leg of the design is exposed to the runt pulse on every
    // sel_q transition.
    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;

endmodule
