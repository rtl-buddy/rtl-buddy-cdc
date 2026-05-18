// Negative-case fixture for RDC-004 — reset pin driven by
// combinational logic with no synchroniser in the path.
//
// Two flops produce per-channel kill signals; the consumer's ARST
// is wired to the AND of both Q outputs. Comb-gate outputs can
// glitch when the inputs transition near-simultaneously — the
// transient appears as a spurious reset assertion that the
// destination flop will respond to.
//
// RDC-004 must fire once on `q_out` (the comb-driven consumer).
// No other rule should fire (single clock, both polarities matched
// so RDC-002 is silent, no clock crossing so RDC-001/003 silent).

module bad_rdc_004_comb_driven_reset (
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

    // BAD: comb-AND of two flop outputs driving an ARST pin.
    logic comb_rst;
    assign comb_rst = flop_a & flop_b;

    always_ff @(posedge clk or negedge comb_rst) begin
        if (!comb_rst) q_out <= 1'b0;
        else           q_out <= d_in;
    end

endmodule
