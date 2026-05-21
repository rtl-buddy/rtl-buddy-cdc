// Positive counterpart to bad_unconstrained_input_derived_clock.
//
// The SDC types `in` against clk_a, and the clk_b capture uses a
// textbook 2FF synchronizer. CDC-011 stays silent because the port is
// typed; CDC-001 stays silent because the async path is synchronized.

module good_unconstrained_input_derived_clock_typed (
    input  logic clk_a,
    input  logic clk_b,
    input  logic clk_c,
    input  logic in,
    output logic q
);

    (* cdc_sync *) logic meta;
    always_ff @(posedge clk_b) begin
        meta <= in;
        q    <= meta;
    end

endmodule
