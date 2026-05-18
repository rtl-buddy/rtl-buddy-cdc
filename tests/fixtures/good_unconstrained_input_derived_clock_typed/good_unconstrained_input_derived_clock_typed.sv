// Positive counterpart to bad_unconstrained_input_derived_clock.
//
// The SDC types `in` against the same clock the derived-clock flop
// resolves to (whichever input of the AND tree trace_clock_root picks
// first). CDC-011 stays silent because the port is typed; CDC-001
// stays silent because the resolved source domain matches the
// destination.

module good_unconstrained_input_derived_clock_typed (
    input  logic clk_a,
    input  logic clk_b,
    input  logic clk_c,
    input  logic in,
    output logic q
);

    wire derived = clk_a & clk_b & clk_c;
    always_ff @(posedge derived) q <= in;

endmodule
