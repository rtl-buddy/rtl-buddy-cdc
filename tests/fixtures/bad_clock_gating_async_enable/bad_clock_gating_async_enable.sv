// Negative-case fixture: a raw-AND clock gate whose enable is a
// flop in a clock domain asynchronous to the gated clock.
//
// Hazard: the enable transitions asynchronously to `clk_a`, so the
// AND output can chop the gated clock into runt pulses every time
// the enable changes. The downstream flop sees a clock with
// arbitrarily short high phases — every flop on this clock branch
// is at risk.
//
// CDC-010 fires on the $and's enable leg B. A bare $and has no named
// control pin; the clock-root walk reads leg A as the clock (clk_a)
// and leg B as the enable, and CDC-010 checks the leg the walk reads
// as control. The enable flop's Q is NOT a second clock: before
// rtl-buddy-cdc#408 the walk followed it back to clk_b as a divided
// clock and reported a two-clock combine (CDC-023, warning) instead
// of the async enable (CDC-010, error).
//
// See issue #177 and rtl-buddy-cdc#408.

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
