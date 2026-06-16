// Coverage fixture for issue #263 — a flop clocked through a transparent
// latch resolves to domain-unknown, so it is silently EXCLUDED from CDC
// analysis.
//
// `unresolved_q` is clocked by `gclk`, which is driven by a $dlatch whose
// D is `clk_a`. The clock-root tracer (domain.py `_trace`) follows
// buffers, clock gates, muxes and flop-Q dividers, but NOT a $dlatch Q —
// so `gclk` (and the flop it clocks) trace to None / `<unresolved>`.
//
// This is the P0 visibility repro: nothing today tells the user that a
// flop was dropped from analysis. P2 will teach the tracer to follow a
// clock-path latch and this flop will then resolve to `clk_a` (the
// fixture expectation moves there, not here).
//
// The design also carries a real clk_a -> clk_b crossing (`a_q` -> `b_q`)
// so the fixture doubles as a parity anchor: adding the visibility
// diagnostic must not perturb the crossing/violation that the analyzer
// already sees.
module bad_unresolved_clock_latch (
    input  logic clk_a,
    input  logic clk_b,
    input  logic en,
    input  logic d_in,
    output logic q_out
);

    // Latch in the clock path -> `gclk` and its flop are domain-unknown.
    logic gclk;
    always_latch
        if (en) gclk = clk_a;

    logic unresolved_q;
    always_ff @(posedge gclk) unresolved_q <= d_in;

    // A genuine cross-domain crossing: clk_a register -> clk_b register,
    // unsynchronised. The crossing walker resolves both endpoints, so this
    // fires independently of the unresolved flop above.
    logic a_q;
    always_ff @(posedge clk_a) a_q <= d_in;
    logic b_q;
    always_ff @(posedge clk_b) b_q <= a_q;

    assign q_out = unresolved_q ^ b_q;

endmodule
