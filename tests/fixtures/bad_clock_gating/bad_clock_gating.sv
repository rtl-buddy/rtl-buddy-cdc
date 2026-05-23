// Negative-case fixture: a raw-AND clock gate driven by a
// combinational (un-registered) enable in the same clock domain.
//
// Hazard: the AND output can glitch on any input transition,
// producing runt pulses on the gated clock. The downstream flop
// can clock at unintended moments.
//
// This is a *synthesis-correctness* issue (use an ICG cell — a
// latch-then-AND structure that filters glitches), not a CDC one:
// no clock-domain crossing occurs. rtl-buddy-cdc is a CDC analyzer,
// so the rule pack stays silent here today. The paired test pins
// that behaviour; if the rule pack grows a glitch detector in the
// future, the test surfaces the change so coverage can be widened
// deliberately.
//
// See issue #177.

module bad_clock_gating (
    input  logic clk,
    input  logic rst_n,
    input  logic en_a,
    input  logic en_b,
    input  logic d,
    output logic q
);

    // Combinational, un-registered enable. No latch in front of the
    // AND, so glitches on en_comb leak into gated_clk.
    logic en_comb;
    assign en_comb = en_a & en_b;

    // Raw-AND clock gate. Yosys keeps this as a $and cell after
    // `proc; flatten` — no ICG inference.
    logic gated_clk;
    assign gated_clk = clk & en_comb;

    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= d;
    end

endmodule
