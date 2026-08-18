// Negative-case fixture for CDC-022 (rtl-buddy-cdc#275).
//
// `xpm_cdc_single` carries its synchroniser stage count as the
// `DEST_SYNC_FF` parameter, not as a flop chain — the stages live
// inside the macro, which the analyzer sees only as a blackbox. Once
// the macro is recognised as a synchroniser its crossing stops being
// reported, so CDC-002 (which walks a real chain) can never speak to
// the depth question again.
//
// Here the instance declares DEST_SYNC_FF=2. Under a project requiring
// `--sync-depth 3` that is short, and CDC-022 is the only rule that can
// say so. At the default `--sync-depth 2` the same design is silent,
// exactly mirroring CDC-002's default-silent posture.

(* blackbox *)
module xpm_cdc_single #(
    parameter integer DEST_SYNC_FF   = 4,
    parameter integer INIT_SYNC_FF   = 0,
    parameter integer SIM_ASSERT_CHK = 0,
    parameter integer SRC_INPUT_REG  = 1
) (
    input  wire src_clk,
    input  wire src_in,
    input  wire dest_clk,
    output wire dest_out
);
endmodule

module bad_xpm_shallow_sync_depth (
    input  wire clk_a,
    input  wire clk_b,
    input  wire flag_in,
    output wire flag_out
);
    reg flag_q;
    always_ff @(posedge clk_a) flag_q <= flag_in;

    wire flag_sync;
    xpm_cdc_single #(
        .DEST_SYNC_FF(2),
        .SRC_INPUT_REG(0)
    ) u_flag_sync (
        .src_clk (clk_a),
        .src_in  (flag_q),
        .dest_clk(clk_b),
        .dest_out(flag_sync)
    );

    reg flag_b;
    always_ff @(posedge clk_b) flag_b <= flag_sync;
    assign flag_out = flag_b;
endmodule
