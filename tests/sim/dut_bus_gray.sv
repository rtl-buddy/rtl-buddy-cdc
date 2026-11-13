// CDC-004 good DUT (rtl-buddy-cdc#331): the counter of
// `dut_bus_binary.sv`, gray-coded at the source (g = b ^ (b >> 1))
// before it crosses the same ungated 4-bit 2FF synchroniser. One bit
// changes per increment, so a lane that resolves a cycle late leaves
// the destination on the previous value, never on a mix.
//
// The analyzer accepts this structurally (gray-encode XOR at the
// source + a lane-aligned multi-bit sync chain): CDC-004 must stay
// silent. The simulation must never show an incoherent sample, as
// long as the source changes at most once per destination period —
// the testbench keeps src_clk slower than dst_clk for that reason.

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_bus_gray (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [3:0] q_out
);
    logic [3:0] bin = 4'd0;
    logic [3:0] bin_next;
    assign bin_next = bin + 4'd1;

    // Source register; the testbench reads it as `u_dut.src_data`.
    logic [3:0] src_data = 4'd0;
    always_ff @(posedge src_clk) begin
        if (inc) begin
            bin      <= bin_next;
            src_data <= bin_next ^ (bin_next >> 1);
        end
    end

    logic [3:0] sync_meta;
    meta_flop_bus #(.WIDTH(4), .RATE_PCT(80), .SEED(32'hB1_0002))
        u_meta (.clk(dst_clk), .d(src_data), .q(sync_meta));

    logic [3:0] sync_q = 4'd0;
    always_ff @(posedge dst_clk) sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
