// Positive counterpart to bad_rdc_005_multi_source_reset.
//
// Same two reset ports, but a `$mux` (synthesised from a ternary)
// explicitly selects which one is active. The selection signal
// (`use_block_rst`) makes the user's intent unambiguous: at any
// given moment exactly one of the two reset sources is in effect.
//
// The selected reset then passes through a local 2-FF reset
// synchroniser in the `clk` domain before reaching the data flop.
// This keeps the fixture clean for both RDC-005 *and* RDC-006:
//
// - RDC-005 must NOT fire — the immediate driver cell of the sync
//   chain's reset is a `$mux`, which is the explicit-muxing
//   exemption (RDC-005's whole point).
// - RDC-006 must NOT fire — the sync chain's flops are recognised
//   reset-synchroniser stages and are exempted; the data flop's
//   ARST is a flop's Q (the sync tail), not a `$mux`, so the rule
//   doesn't trigger on it either.
//
// The fixture's purpose is preserved: it still demonstrates the
// muxed-reset exemption. The extra sync stage is what real designs
// also do, and is the textbook fix RDC-006 prescribes.

module good_rdc_005_muxed_reset (
    input  logic clk,
    input  logic global_rst_n,
    input  logic block_rst_n,
    input  logic use_block_rst,
    input  logic d_in,
    output logic q_out
);

    logic selected_rst_n;
    assign selected_rst_n = use_block_rst ? block_rst_n : global_rst_n;

    logic rst_meta_n;
    logic rst_sync_n;
    always_ff @(posedge clk or negedge selected_rst_n) begin
        if (!selected_rst_n) begin
            rst_meta_n <= 1'b0;
            rst_sync_n <= 1'b0;
        end else begin
            rst_meta_n <= 1'b1;
            rst_sync_n <= rst_meta_n;
        end
    end

    always_ff @(posedge clk or negedge rst_sync_n) begin
        if (!rst_sync_n) q_out <= 1'b0;
        else             q_out <= d_in;
    end

endmodule
