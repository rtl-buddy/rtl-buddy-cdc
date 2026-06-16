// icg_port_enable — the COMMON, safe ICG shape and the regression guard
// for the clock-combining decline (#263): a latch-based integrated clock
// gate whose clock is on D and whose ENABLE comes from a top-level input
// port (`en`). Because `en` is NOT a declared clock, this is a single
// clock + enable — it must STILL resolve to `clk` (NOT be mistaken for a
// two-clock combine and declined). Guards against the obvious-but-wrong
// "decline whenever two legs resolve" fix, which would wrongly strand
// every port-enabled clock gate.
module icg_port_enable (
    input  wire clk,
    input  wire en,
    input  wire d,
    output reg  q
);
  reg gclk;
  always @(*) if (en) gclk = clk;     // $dlatch: D=clk, EN=en (a port, not a clock)
  always @(posedge gclk) q <= d;
endmodule
