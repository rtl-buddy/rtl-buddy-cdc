// Negative-case fixture for CDC-011 (#97), multi-bit shape.
//
// 8-bit input `in[7:0]` has no `set_input_delay -clock` typing and is
// captured by flop banks in two distinct clock domains (`clk_a` and
// `clk_b`). Vector variant of `bad_unconstrained_input_two_domains` —
// guards against the rule firing per-bit and either deduplicating
// wrong or missing the bus shape entirely. Expected: one error-
// severity CDC-011 violation naming both destination clocks.

module bad_unconstrained_input_bus_two_domains (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic [7:0] in,
    output logic [7:0] q_a,
    output logic [7:0] q_b
);

    always_ff @(posedge clk_a) q_a <= in;
    always_ff @(posedge clk_b) q_b <= in;

endmodule
