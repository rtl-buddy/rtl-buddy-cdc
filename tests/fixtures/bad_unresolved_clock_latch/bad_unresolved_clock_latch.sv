// Coverage fixture for issue #263 — a flop clocked through a transparent
// latch in the CLOCK path.
//
// `unresolved_q` is clocked by `gclk`, which is driven by a $dlatch whose
// D is `clk_a`. Before P2 the clock-root tracer (domain.py `_trace`)
// followed buffers, clock gates, muxes and flop-Q dividers but NOT a
// $dlatch Q, so `gclk` (and the flop it clocks) traced to None — the flop
// was silently EXCLUDED from CDC analysis. That under-resolution is what
// the P0 visibility diagnostic (`summary.domain_unknown`) surfaced.
//
// P2 taught the tracer to follow a clock-path latch (explore the latch's
// D and EN pins), so `unresolved_q` now RESOLVES to `clk_a` and the
// fixture reports `domain_unknown == 0`. The name is kept for history;
// the durable `domain_unknown > 0` anchor is `deep_clock_divider_chain`.
//
// The design also carries a real clk_a -> clk_b crossing (`a_q` -> `b_q`)
// so the fixture doubles as a parity anchor: resolving the latch-clocked
// flop must not perturb the crossing/violation that the analyzer already
// sees.
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
