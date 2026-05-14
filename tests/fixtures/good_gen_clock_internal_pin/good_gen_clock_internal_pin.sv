// Regression fixture for issue #15: gen-clock target on a child
// instance's output port pin must resolve under both frontends.
//
// The child module's output port is driven by an internal divider
// flop via a continuous assign. After flattening, the SDC's
// `[get_pins u_c/clk_out]` must resolve to a netname that the
// `_build_bit_to_clock` lookup can find. Yosys-flatten + opt_clean
// preserves both the port netname (`u_c.clk_out`) and the driver
// (`u_c.div`) as aliases of the same bits; the slang frontend
// originally only emitted the driver, which silently dropped the
// gen-clock and collapsed the downstream flop's domain back to
// `ck_in`. This fixture exists to prevent that regression.
//
// Topology: a 1-deep clock-divider chain with one downstream flop.
//
//   ck_in ──► u_c (div2) ──► ck_div ──► q_out
//                ▲
//                │ create_generated_clock at u_c/clk_out
//
// Expected (under both frontends):
//   - 3 flops total (2 on ck_in inside u_c, 1 on ck_div for q_out).
//   - 2 cross-domain crossings detected (the div feedback + the data
//     hop into q_out).
//   - 0 violations (synchronous via -master_clock chain back to ck_in).

module child (
  input  logic clk_in,
  input  logic rst_n,
  output logic clk_out,
  input  logic d,
  output logic q
);
  logic div;
  always_ff @(posedge clk_in or negedge rst_n) begin
    if (!rst_n) div <= 1'b0;
    else        div <= ~div;
  end
  assign clk_out = div;          // alias output port to internal var

  always_ff @(posedge clk_in or negedge rst_n) begin
    if (!rst_n) q <= 1'b0;
    else        q <= d;
  end
endmodule

module good_gen_clock_internal_pin (
  input  logic clk,
  input  logic rst_n,
  input  logic d,
  output logic q_out
);
  logic ck_div, qmid;
  child u_c (.clk_in(clk), .rst_n(rst_n), .clk_out(ck_div), .d(d), .q(qmid));
  always_ff @(posedge ck_div or negedge rst_n) begin
    if (!rst_n) q_out <= 1'b0;
    else        q_out <= qmid;
  end
endmodule
