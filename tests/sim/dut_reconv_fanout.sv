// CDC-005 bad DUT (rtl-buddy-cdc#331): one source bit fans out to two
// independent 2FF synchronisers in dst_clk, and a destination register
// recombines the two synchronised copies. The source only ever
// presents the pair (x, x); each chain's first stage resolves on its
// own, so on a toggle one copy can arrive a cycle before the other
// and the recombination register captures (1, 0) or (0, 1).
//
// The analyzer sees one source flop driving two sync chains whose
// outputs reconverge: CDC-005 must fire. The simulation must show the
// mismatched pair.

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_reconv_fanout (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [1:0] q_out
);
    logic src_q = 1'b0;
    always_ff @(posedge src_clk) if (inc) src_q <= ~src_q;

    // What the source presents to the two consumers; the testbench
    // reads it as `u_dut.src_data`. A wire, not a register.
    wire [1:0] src_data = {src_q, src_q};

    logic a_meta, b_meta;
    logic a_sync = 1'b0;
    logic b_sync = 1'b0;
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0011))
        u_a_meta (.clk(dst_clk), .d(src_q), .q(a_meta));
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0012))
        u_b_meta (.clk(dst_clk), .d(src_q), .q(b_meta));
    always_ff @(posedge dst_clk) begin
        a_sync <= a_meta;
        b_sync <= b_meta;
    end

    // Recombination point: one register observes both chains.
    logic [1:0] pair_q = 2'b00;
    always_ff @(posedge dst_clk) pair_q <= {a_sync, b_sync};

    assign q_out = pair_q;
endmodule
