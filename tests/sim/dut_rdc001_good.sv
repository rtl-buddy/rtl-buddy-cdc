// RDC-001 good case: same foreign-domain reset, but routed through
// a 2-stage reset synchroniser in the destination clock domain
// before reaching the dst-flop's ARST. The deassertion edge now
// meets recovery/removal against dst_clk.

`include "meta_flop_lib.sv"

module dut_rdc001_good (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic local_rst_req,
    input  logic d_in,
    output logic q_out
);
    logic local_rst_n;
    safe_flop_arst u_src_rst (
        .clk    (src_clk),
        .arst_n (global_rst_n),
        .d      (~local_rst_req),
        .q      (local_rst_n)
    );

    // Reset synchroniser in dst_clk's domain. Async assertion
    // (ARST tied to local_rst_n), sync deassertion (chain head D
    // tied to constant 1).
    logic sync_a;
    safe_flop_arst u_rsync_a (
        .clk    (dst_clk),
        .arst_n (local_rst_n),
        .d      (1'b1),
        .q      (sync_a)
    );
    logic sync_b;
    safe_flop_arst u_rsync_b (
        .clk    (dst_clk),
        .arst_n (local_rst_n),
        .d      (sync_a),
        .q      (sync_b)
    );

    // Destination flop now uses the synchronised reset.
    // ARST_IS_ASYNC=0 because sync_b is driven by a dst_clk-domain
    // flop — the reset sync chain has converted the foreign-domain
    // edge into a same-clock release edge.
    meta_flop_arst #(.RATE_PCT(60), .SEED(32'hF00D_BEEF),
                     .ARST_IS_ASYNC(0)) u_dst (
        .clk    (dst_clk),
        .arst_n (sync_b),
        .d      (d_in),
        .q      (q_out)
    );
endmodule
