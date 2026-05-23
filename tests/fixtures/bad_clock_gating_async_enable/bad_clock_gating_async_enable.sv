// Negative-case fixture: a raw-AND clock gate whose enable is a
// flop in a clock domain asynchronous to the gated clock.
//
// Hazard: the enable transitions asynchronously to `clk_a`, so the
// AND output can chop the gated clock into runt pulses every time
// the enable changes. The downstream flop sees a clock with
// arbitrarily short high phases — every flop on this clock branch
// is at risk.
//
// CDC-010 today targets named-control-pin cells ($mux.S, $dffe.EN,
// $dlatch.EN) and the pin-name heuristic for tech-mapped library
// cells (E / EN / CE / GATE / SE). A bare $and cell has none of
// those, so this hazard is currently invisible to the rule pack.
// The paired test pins that behaviour so future CDC-010 extensions
// (or a dedicated rule) can re-evaluate; if a rule starts firing
// here, the test signals the coverage expansion.
//
// See issue #177.

module bad_clock_gating_async_enable (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_a_n,
    input  logic rst_b_n,
    input  logic en_in,
    input  logic d,
    output logic q
);

    // Enable lives in clk_b's domain — fully asynchronous to clk_a.
    logic en_b_q;
    always_ff @(posedge clk_b or negedge rst_b_n) begin
        if (!rst_b_n) en_b_q <= 1'b0;
        else          en_b_q <= en_in;
    end

    // Raw-AND clock gate driven by a foreign-domain enable.
    logic gated_clk;
    assign gated_clk = clk_a & en_b_q;

    // Captured flop lives on the gated branch of clk_a.
    always_ff @(posedge gated_clk or negedge rst_a_n) begin
        if (!rst_a_n) q <= 1'b0;
        else          q <= d;
    end

endmodule
