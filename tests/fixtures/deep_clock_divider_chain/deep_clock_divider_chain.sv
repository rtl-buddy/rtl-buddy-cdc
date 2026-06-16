// Coverage fixture for issue #263 (P1) — a clock that reaches a flop only
// through a LONG chain of divider flops resolves to domain-unknown at the
// default trace depth (16) and RESOLVES once `--clock-trace-depth` is raised.
//
// `clk_a` feeds a 30-stage ripple divider: each `div[i]` flop toggles on the
// rising edge of `div[i-1]` (its Q clocks the next stage). The deepest tap
// `div[29]` then clocks `deep_q`. The clock-root tracer (domain.py `_trace`)
// follows a flop-Q divider by recursing on that flop's CLK pin, costing one
// hop per stage — so reaching `clk_a` from `deep_q`'s CLK needs ~30 hops.
// At the default budget of 16 the walk gives up and `deep_q` is
// domain-unknown (counted in `summary.domain_unknown`). At
// `--clock-trace-depth 40` the same walk reaches `clk_a` and `deep_q`
// resolves to the `clk_a` domain — raising the budget only ever resolves
// MORE flops, never fewer.
//
// The design also carries a genuine clk_a -> clk_b crossing (`a_q` -> `b_q`,
// unsynchronised) whose endpoints both resolve at ANY depth >= 1. That makes
// the fixture a parity anchor: the crossing/violation the analyzer sees must
// be byte-identical at depth 16 and depth 40 — the deeper walk adds a
// resolved domain, it does not perturb the crossing already found.
module deep_clock_divider_chain (
    input  logic clk_a,
    input  logic clk_b,
    input  logic d_in,
    output logic q_out
);

    // 30-stage ripple divider off clk_a. Each stage toggles on the prior
    // stage's Q, so div[i]'s clock root is i+1 divider hops above clk_a.
    localparam int STAGES = 30;
    logic [STAGES-1:0] div;

    always_ff @(posedge clk_a) div[0] <= ~div[0];
    genvar gi;
    generate
        for (gi = 1; gi < STAGES; gi++) begin : g_div
            always_ff @(posedge div[gi-1]) div[gi] <= ~div[gi];
        end
    endgenerate

    // Clocked by the deepest divider tap — ~30 hops from clk_a, so beyond
    // the default-16 trace budget: domain-unknown by default, clk_a at depth 40.
    logic deep_q;
    always_ff @(posedge div[STAGES-1]) deep_q <= d_in;

    // Genuine clk_a -> clk_b crossing, resolvable at any depth. Parity anchor.
    logic a_q;
    always_ff @(posedge clk_a) a_q <= d_in;
    logic b_q;
    always_ff @(posedge clk_b) b_q <= a_q;

    assign q_out = deep_q ^ b_q;

endmodule
