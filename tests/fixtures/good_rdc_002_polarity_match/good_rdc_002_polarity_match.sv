// Positive counterpart to bad_rdc_002_polarity_mismatch.
//
// Same gated-reset topology, but the consumer matches the producer's
// polarity. Both flops are active-low reset (ARST_POLARITY=0,
// ARST_VALUE=0). When raw_rst_n is asserted:
//
//   gated_rst.Q → 0  (the assert value)
//   q_out sees ARST=0, q_out's polarity=0 → q_out enters reset.  ✓
//
// RDC-002 must stay silent.

module good_rdc_002_polarity_match (
    input  logic clk,
    input  logic raw_rst_n,
    output logic q_out
);

    logic gated_rst;
    always_ff @(posedge clk or negedge raw_rst_n) begin
        if (!raw_rst_n) gated_rst <= 1'b0;
        else            gated_rst <= 1'b1;
    end

    always_ff @(posedge clk or negedge gated_rst) begin
        if (!gated_rst) q_out <= 1'b0;
        else            q_out <= 1'b1;
    end

endmodule
