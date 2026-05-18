// Positive counterpart to bad_rdc_004_comb_driven_reset.
//
// Same per-channel kill flops, but the AND is registered on `clk`
// before being used as a reset. The reset edge is glitch-free —
// it's the synchronous output of a flop, not a comb gate. All
// polarities are matched (every flop is active-low, ARST_VALUE=0)
// so RDC-002 stays silent too.
//
// RDC-004 must NOT fire (the consumer's ARST is directly driven by
// `comb_rst_reg`'s Q, classified as ``source="inferred"``).

module good_rdc_004_registered_reset (
    input  logic clk,
    input  logic global_rst_n,
    input  logic kill_a,
    input  logic kill_b,
    input  logic d_in,
    output logic q_out
);

    logic flop_a, flop_b;
    always_ff @(posedge clk or negedge global_rst_n) begin
        if (!global_rst_n) begin
            flop_a <= 1'b0;
            flop_b <= 1'b0;
        end else begin
            flop_a <= ~kill_a;
            flop_b <= ~kill_b;
        end
    end

    // FIX: register the AND on `clk` before using it as a reset.
    logic comb_rst_reg;
    always_ff @(posedge clk or negedge global_rst_n) begin
        if (!global_rst_n) comb_rst_reg <= 1'b0;
        else               comb_rst_reg <= flop_a & flop_b;
    end

    always_ff @(posedge clk or negedge comb_rst_reg) begin
        if (!comb_rst_reg) q_out <= 1'b0;
        else               q_out <= d_in;
    end

endmodule
