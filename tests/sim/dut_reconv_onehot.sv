// CDC-005 bad DUT (rtl-buddy-cdc#331), the related-bits form: a
// one-hot 2-bit state flips 01 <-> 10, both bits changing on the same
// src_clk edge. Each bit crosses through its own 2FF synchroniser and
// a destination register recombines them. When one lane resolves a
// cycle before the other, the recombination register captures 00 or
// 11 — a state the source never held.
//
// The analyzer reports the reconvergence (CDC-005, the 2-bit state
// register is the one source flop of both chains) and the sliced
// multi-bit source (CDC-020). The simulation must show the illegal
// pairs.

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_reconv_onehot (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [1:0] q_out
);
    // One-hot state; the testbench reads it as `u_dut.src_data`.
    logic [1:0] src_data = 2'b01;
    always_ff @(posedge src_clk) if (inc) src_data <= {src_data[0], src_data[1]};

    logic a_meta, b_meta;
    logic a_sync = 1'b0;
    logic b_sync = 1'b1;
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0031))
        u_a_meta (.clk(dst_clk), .d(src_data[1]), .q(a_meta));
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0032))
        u_b_meta (.clk(dst_clk), .d(src_data[0]), .q(b_meta));
    always_ff @(posedge dst_clk) begin
        a_sync <= a_meta;
        b_sync <= b_meta;
    end

    // Recombination point: one register observes both chains.
    logic [1:0] pair_q = 2'b01;
    always_ff @(posedge dst_clk) pair_q <= {a_sync, b_sync};

    assign q_out = pair_q;
endmodule
