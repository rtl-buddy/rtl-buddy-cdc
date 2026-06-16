// clock_combine_generated — a DECLARED generated clock (gdiv, a /2
// divide of clkA via create_generated_clock) combined with a different
// real clock (clkB) on a gate: `gclk = gdiv & clkB`. Two distinct
// declared clock identities meet on one net, so the downstream flop is
// genuinely ambiguous and the tracer DECLINES (domain-unknown), exactly
// as for two create_clock clocks — issue #263. Exercises the
// generated-clock leg of the clock-identity predicate.
module clock_combine_generated (
    input  wire clkA,
    input  wire clkB,
    input  wire din,
    output reg  q
);
  reg gdiv;
  always @(posedge clkA) gdiv <= ~gdiv;   // /2 divider net — declared as a generated clock
  wire gclk = gdiv & clkB;                // combine generated gdiv with real clkB
  always @(posedge gclk) q <= din;
endmodule
