// Over-suppression guard for XPM CDC macro recognition (rtl-buddy-cdc#275).
//
// Recognising `xpm_cdc_single` as a synchroniser must accept the
// crossing the macro HANDLES without blinding the analyzer to the one
// it does not. The macro synchronises clk_a -> clk_b. Its `dest_out`
// then fans out to TWO consumers:
//
//   - `flag_b` in clk_b — the domain the macro delivered into. Safe,
//     and correctly silent.
//   - `flag_c` in clk_c — a THIRD domain the macro knows nothing
//     about. That is a genuine unsynchronised crossing and CDC-001
//     must still fire on it.
//
// The mechanism: the boundary summary stamps `dest_out` with the
// resolved `dest_clk` root (clk_b), so find_crossings' ordinary
// `dst_clock != src_clock` test emits the clk_b -> clk_c crossing while
// dropping the clk_b -> clk_b one. Suppression is a consequence of
// naming the right domain, not of an unconditional skip.

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

module xpm_cdc_third_domain (
    input  wire clk_a,
    input  wire clk_b,
    input  wire clk_c,
    input  wire flag_in,
    output wire flag_b_out,
    output wire flag_c_out
);
    reg flag_q;
    always_ff @(posedge clk_a) flag_q <= flag_in;

    wire flag_sync;
    xpm_cdc_single #(
        .DEST_SYNC_FF(4),
        .SRC_INPUT_REG(0)
    ) u_flag_sync (
        .src_clk (clk_a),
        .src_in  (flag_q),
        .dest_clk(clk_b),
        .dest_out(flag_sync)
    );

    // Legitimate consumer: the domain the macro delivered into.
    reg flag_b;
    always_ff @(posedge clk_b) flag_b <= flag_sync;

    // Illegitimate consumer: a third domain, unsynchronised.
    reg flag_c;
    always_ff @(posedge clk_c) flag_c <= flag_sync;

    assign flag_b_out = flag_b;
    assign flag_c_out = flag_c;
endmodule
