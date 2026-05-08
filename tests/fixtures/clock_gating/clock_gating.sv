// Positive-case fixture: an integrated-clock-gate (ICG) drives a
// flop's CLK pin. The flop is still in the `clk` domain — the gate
// only suppresses some edges. The analyzer should resolve the gated
// clock back to its source port instead of marking the flop as
// "<unresolved>".

module clock_gating (
    input  logic clk,
    input  logic en,
    input  logic rst_n,
    input  logic d,
    output logic q
);

    // Latch-based ICG: ``en`` is sampled on the low phase of clk and
    // AND'd with clk to produce the gated clock. (Glitch-free pattern;
    // not strictly required for this test, but realistic.)
    logic en_latched;
    always_latch begin
        if (~clk) en_latched = en;
    end
    logic gated_clk;
    assign gated_clk = clk & en_latched;

    // The flop runs on the *gated* clock, but its functional domain
    // is still ``clk``.
    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= d;
    end

endmodule
