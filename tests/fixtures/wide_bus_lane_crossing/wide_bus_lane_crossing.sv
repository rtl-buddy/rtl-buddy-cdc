// Wide (16-bit) LANE-ALIGNED bus crossing: src_clk -> dst_clk through a bitwise
// AND ($and). Each destination lane i depends only on source lane i, so the
// lane-aware data-fanout (issue #258) propagates bit-precisely (src[i] ->
// Y[i] -> dst[i]) instead of fanning each source bit across all 16 outputs.
// The reported crossing must be unchanged: one src->dst bus crossing, width 16.
module wide_bus_lane_crossing (
    input  wire        src_clk,
    input  wire        dst_clk,
    input  wire [15:0] din,
    input  wire [15:0] mask,
    output wire [15:0] dout
);
  reg [15:0] src;
  always @(posedge src_clk) src <= din;
  wire [15:0] masked = src & mask;   // $and — lane-aligned (Y[i] = src[i] & mask[i])
  reg [15:0] dst;
  always @(posedge dst_clk) dst <= masked;
  assign dout = dst;
endmodule
