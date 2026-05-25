// Bad DUT: single-bit crossing with no synchroniser, wrapped in the
// metastability-injection library so simulation can observe the
// failure mode the analyzer flags as CDC-001.
//
// Source-domain flop is a `safe_flop` (its D comes from a
// dst-synchronous testbench input — no metastability there). The
// destination flop is a `meta_flop`: when src_q transitions, sim
// rolls the dice and may emit a random bit on that cycle.

`include "meta_flop_lib.sv"

module dut_unsynced (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    safe_flop #(.SEED(32'hA5A5_0001))
        u_src (.clk(src_clk), .d(d_in), .q(src_q));

    // The crossing: dst-domain flop sampling a src-domain signal
    // with no upstream synchroniser. Under injection, q_out can
    // differ from a delayed copy of d_in.
    meta_flop #(.RATE_PCT(80), .SEED(32'h1234_5678))
        u_dst (.clk(dst_clk), .d(src_q), .q(q_out));
endmodule
