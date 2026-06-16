// inferred_gate_clock — an undeclared internal clock whose driver is a
// clock GATE / ICG output (not a flop Q). `gclk = clk & en` fans out to
// four separate flop CLK pins and is never declared with
// create_generated_clock, so it is reported as an inferred-clock
// candidate with driver_kind="gate" (issue #263, P3). Advisory only:
// the four flops still resolve to `clk` via the gate clause, so no
// domain or crossing changes — this fixture exercises the gate-driver
// branch of candidate detection. Four single-bit always blocks keep
// yosys from packing them into one multi-bit flop.
module inferred_gate_clock (
    input  wire clk,
    input  wire en,
    input  wire din,
    output wire q0, q1, q2, q3
);
  wire gclk = clk & en;            // ICG/gate output — undeclared gated clock
  reg  r0, r1, r2, r3;
  always @(posedge gclk) r0 <= din;
  always @(posedge gclk) r1 <= r0;
  always @(posedge gclk) r2 <= r1;
  always @(posedge gclk) r3 <= r2;
  assign q0 = r0; assign q1 = r1; assign q2 = r2; assign q3 = r3;
endmodule
