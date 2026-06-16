// clock_combine_gate — a two-input gate that COMBINES two distinct
// declared clocks (clkA & clkB) into one net feeding a flop's clock.
// Like clock_combine_latch but the combining cell is a $and gate rather
// than a latch. The analyzer must DECLINE (domain-unknown) instead of
// silently picking one leg — issue #263. A normal clock GATE (clock +
// non-clock enable) still resolves; only a genuine two-declared-clock
// combine declines.
module clock_combine_gate (
    input  wire clkA,
    input  wire clkB,
    input  wire d,
    output reg  q
);
  wire gclk = clkA & clkB;            // $and: A=clkA, B=clkB
  always @(posedge gclk) q <= d;
endmodule
