// Negative-case fixture for the --reset-hints YAML path (issue #129).
//
// Same shape as bad_marked_reset_polarity, but the (* reset_polarity *)
// SV attribute is intentionally absent — the port-declared polarity
// has to come from an external YAML hints file instead.
//
// Without the hint, the analyzer has nothing to compare the flop's
// inferred ARST_POLARITY against (no producer flop, no clock-domain
// crossing). With the hint declaring rst_n as active-low, RDC-002
// fires identically to the SV-attribute case.

module bad_hints_reset_polarity (
    input  logic clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    // Wired posedge: Yosys infers ARST_POLARITY=1. Disagrees with the
    // hint-declared "low" polarity (carried in the companion .hints.yaml).
    logic bad_q;
    always_ff @(posedge clk or posedge rst_n) begin
        if (rst_n) bad_q <= 1'b0;
        else       bad_q <= d_in;
    end

    assign q_out = bad_q;

endmodule
