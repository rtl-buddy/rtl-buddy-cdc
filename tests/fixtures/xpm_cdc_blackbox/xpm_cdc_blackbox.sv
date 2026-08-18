// XPM CDC macro recognition (rtl-buddy-cdc#275).
//
// The Xilinx XPM CDC library is how real Vivado designs synchronise:
// `xpm_cdc_single` for a control bit, `xpm_cdc_gray` for a counter.
// Their sources live in the vendor install tree, so a filelist built
// from project RTL carries only the instantiation — the macro arrives
// as a dual-clock BLACKBOX (`src_clk` + `dest_clk`).
//
// Without primitive recognition that blackbox is "not provably
// single-clock", so it is declined and reported as a CDC-BBX error per
// instance, while the crossing through it silently vanishes. With
// recognition the macro is summarised as a synchroniser: its outputs
// are stamped in the `dest_clk` domain and its data inputs seed no
// virtual sink, so the design reports CLEAN.
//
// The stubs below are port/parameter-faithful to UG974 and deliberately
// bodyless — that is exactly what the analyzer sees in production.

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

(* blackbox *)
module xpm_cdc_gray #(
    parameter integer DEST_SYNC_FF          = 4,
    parameter integer INIT_SYNC_FF          = 0,
    parameter integer REG_OUTPUT            = 0,
    parameter integer SIM_ASSERT_CHK        = 0,
    parameter integer SIM_LOSSLESS_GRAY_CHK = 0,
    parameter integer WIDTH                 = 2
) (
    input  wire             src_clk,
    input  wire [WIDTH-1:0] src_in_bin,
    input  wire             dest_clk,
    output wire [WIDTH-1:0] dest_out_bin
);
endmodule

module xpm_cdc_blackbox (
    input  wire       clk_a,
    input  wire       clk_b,
    input  wire       flag_in,
    input  wire [3:0] cnt_in,
    output wire       flag_out,
    output wire [3:0] cnt_out
);
    // clk_a source registers.
    reg       flag_q;
    reg [3:0] cnt_q;
    always_ff @(posedge clk_a) flag_q <= flag_in;
    always_ff @(posedge clk_a) cnt_q  <= cnt_in;

    // The two crossings, both handled by an XPM macro.
    wire       flag_sync;
    wire [3:0] cnt_sync;

    xpm_cdc_single #(
        .DEST_SYNC_FF(4),
        .SRC_INPUT_REG(0)
    ) u_flag_sync (
        .src_clk (clk_a),
        .src_in  (flag_q),
        .dest_clk(clk_b),
        .dest_out(flag_sync)
    );

    xpm_cdc_gray #(
        .DEST_SYNC_FF(4),
        .WIDTH(4)
    ) u_cnt_sync (
        .src_clk     (clk_a),
        .src_in_bin  (cnt_q),
        .dest_clk    (clk_b),
        .dest_out_bin(cnt_sync)
    );

    // clk_b consumers of the synchronised values.
    reg       flag_b;
    reg [3:0] cnt_b;
    always_ff @(posedge clk_b) flag_b <= flag_sync;
    always_ff @(posedge clk_b) cnt_b  <= cnt_sync;

    assign flag_out = flag_b;
    assign cnt_out  = cnt_b;
endmodule
