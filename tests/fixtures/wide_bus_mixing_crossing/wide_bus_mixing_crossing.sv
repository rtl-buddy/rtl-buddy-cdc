// Wide (16-bit) LANE-MIXING bus crossing: src_clk -> dst_clk through an adder
// ($add), whose carry chain makes output lane i depend on source lanes <= i.
// $add is NOT lane-aligned, so the lane-aware data-fanout (issue #258) must
// fall back to the conservative all-outputs walk and still detect the crossing.
// Guards against the optimization dropping a genuine cross-lane path: the
// reported crossing must be one src->dst bus crossing, width 16.
module wide_bus_mixing_crossing (
    input  wire        src_clk,
    input  wire        dst_clk,
    input  wire [15:0] din,
    input  wire [15:0] addend,
    output wire [15:0] dout
);
  reg [15:0] src;
  always @(posedge src_clk) src <= din;
  wire [15:0] summed = src + addend;   // $add — carry mixes lanes
  reg [15:0] dst;
  always @(posedge dst_clk) dst <= summed;
  assign dout = dst;
endmodule
