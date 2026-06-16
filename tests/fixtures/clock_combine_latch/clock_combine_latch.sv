// clock_combine_latch — a transparent latch that COMBINES two distinct
// declared clocks on its two legs (D = clkA, EN = clkB). This is a real
// clock-mixing node: the gated net toggles on both clocks, so the
// downstream flop's clock domain is genuinely ambiguous. The analyzer
// must DECLINE to resolve it (leaving the flop domain-unknown, surfaced
// by the under-resolution report) rather than silently asserting one leg
// — see issue #263. Contrast clock_through_latch (a single clock routed
// through a latch, which DOES resolve).
module clock_combine_latch (
    input  wire clkA,
    input  wire clkB,
    input  wire d,
    output reg  q
);
  reg gclk;
  always @(*) if (clkB) gclk = clkA;  // $dlatch: D=clkA, EN=clkB
  always @(posedge gclk) q <= d;
endmodule
