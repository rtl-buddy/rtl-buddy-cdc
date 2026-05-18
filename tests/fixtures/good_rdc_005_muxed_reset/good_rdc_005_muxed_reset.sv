// Positive counterpart to bad_rdc_005_multi_source_reset.
//
// Same two reset ports, but a `$mux` (synthesised from a ternary)
// explicitly selects which one is active. The selection signal
// (`use_block_rst`) makes the user's intent unambiguous: at any
// given moment exactly one of the two reset sources is in effect.
//
// RDC-005 must NOT fire — the immediate driver cell of the
// consumer's reset is a `$mux`, which is the explicit-muxing
// exemption.

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

    always_ff @(posedge clk or negedge selected_rst_n) begin
        if (!selected_rst_n) q_out <= 1'b0;
        else                 q_out <= d_in;
    end

endmodule
