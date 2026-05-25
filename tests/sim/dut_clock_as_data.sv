// CDC-008 sim DUT: a flop in main_clk's domain samples another clock
// (snoop_clk) as data. The standard meta_flop methodology applies —
// every time snoop_clk transitions, the main_clk sample of it can
// be metastable. Over a long run with no synchroniser, the captured
// value diverges from a "golden" sample (a one-cycle-delayed copy)
// far more often than for a clean dst-synchronous signal.

`include "meta_flop_lib.sv"

module dut_clock_as_data (
    input  logic src_clk,   // main_clk in CDC-008 terms
    input  logic dst_clk,   // snoop_clk — the foreign clock being used as data
    input  logic d_in,      // unused (TB drives it but the DUT samples a clock)
    output logic q_out
);
    // The bug: sample dst_clk (a foreign-domain clock) as data on
    // src_clk. The cell-level inference is a $dff whose D pin is
    // the snoop_clk net — exactly what CDC-008's clock-network walk
    // catches structurally.
    meta_flop #(.RATE_PCT(80), .SEED(32'h0FF1_CE_42))
        u_dst (.clk(src_clk), .d(dst_clk), .q(q_out));

    // Tie d_in to a no-op so the TB's stimulus doesn't get optimised
    // away (Icarus is OK either way but keep the port active).
    /* verilator lint_off UNUSED */
    wire _unused_d_in = d_in;
    /* verilator lint_on UNUSED */
endmodule
