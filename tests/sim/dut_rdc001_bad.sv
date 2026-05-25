// RDC-001 bad case: foreign-domain async reset drives dst-flop ARST
// without a reset synchroniser. Deassertion edge falls
// asynchronously to dst_clk, exposing recovery/removal violations
// modelled by ``meta_flop_arst``.

`include "meta_flop_lib.sv"

module dut_rdc001_bad (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic local_rst_req,
    input  logic d_in,
    output logic q_out
);
    // Source-domain reset generator: a flop clocked by src_clk that
    // emits an active-low reset whenever local_rst_req is asserted.
    logic local_rst_n;
    safe_flop_arst u_src_rst (
        .clk    (src_clk),
        .arst_n (global_rst_n),
        .d      (~local_rst_req),
        .q      (local_rst_n)
    );

    // Destination flop with a foreign-domain ARST. No reset
    // synchroniser between local_rst_n and the dst_clk-domain flop.
    // ARST_IS_ASYNC=1 because local_rst_n is sourced from a
    // src_clk-domain flop — RDC-001's exact shape.
    meta_flop_arst #(.RATE_PCT(60), .SEED(32'hF00D_BEEF),
                     .ARST_IS_ASYNC(1)) u_dst (
        .clk    (dst_clk),
        .arst_n (local_rst_n),
        .d      (d_in),
        .q      (q_out)
    );
endmodule
