// Coverage fixture for issue #263 (P2) — a clock routed THROUGH a transparent
// latch now resolves to its upstream clock root, where it was domain-unknown
// before.
//
// The clock-root tracer (domain.py `_trace`) follows buffers, two-input clock
// gates, muxes and flop-Q dividers. P2 adds the clock-path latch: when a flop's
// CLK net is driven by a `$dlatch` (or gate-level `$_DLATCH_*`) Q, the walk
// explores the latch's data pin (D) and enable pin (EN coarse / E gate-level),
// returning whichever leg resolves to a clock root.
//
// Two ICG coding styles, both resolving to `clk_a`:
//
//   - D-leg: `always_latch if (en) gclk_d = clk_a;` puts the clock on the
//     latch's D pin. `dq` is clocked by `gclk_d`.
//   - EN-leg: a latch whose ENABLE is the (already-gated) clock and whose D is
//     a steady level. `eq` is clocked by `gclk_en`. Modelled so the clock
//     reaches the latch on the enable pin, exercising the EN/E exploration leg.
//
// Both `dq` and `eq` therefore resolve to `clk_a` (they were domain-unknown at
// P0/P1). The design also carries a genuine clk_a -> clk_b crossing
// (`a_q` -> `b_q`, unsynchronised) whose endpoints resolve at any depth — the
// parity anchor proving the new latch tracing adds a resolved domain without
// perturbing the crossing the analyzer already sees.
module clock_through_latch (
    input  logic clk_a,
    input  logic clk_b,
    input  logic en,
    input  logic d_in,
    output logic q_out
);

    // D-leg ICG: the clock enters the latch on D, gated by `en`.
    logic gclk_d;
    always_latch
        if (en) gclk_d = clk_a;

    logic dq;
    always_ff @(posedge gclk_d) dq <= d_in;

    // EN-leg ICG: a gated clock drives the latch ENABLE; the latch passes a
    // steady level through while enabled. Only the if-branch assigns, so yosys
    // infers a real `$dlatch` whose EN is the gated clock and whose D is a
    // constant level. The constant D does NOT trace to any clock, so resolution
    // must follow the ENABLE pin back to `clk_a` — this is the leg the D-leg
    // case never exercises.
    logic gclk_pre;
    assign gclk_pre = clk_a & en;  // a real gated clock on the enable leg

    logic gclk_en;
    always_latch
        if (gclk_pre) gclk_en = 1'b1;

    logic eq;
    always_ff @(posedge gclk_en) eq <= d_in;

    // Genuine clk_a -> clk_b crossing, resolvable at any depth. Parity anchor.
    logic a_q;
    always_ff @(posedge clk_a) a_q <= d_in;
    logic b_q;
    always_ff @(posedge clk_b) b_q <= a_q;

    assign q_out = dq ^ eq ^ b_q;

endmodule
