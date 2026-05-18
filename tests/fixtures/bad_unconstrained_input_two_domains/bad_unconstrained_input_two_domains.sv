// Regression-baseline fixture for issue #97 (CDC-011, pending).
//
// Scalar input `in` has no `set_input_delay -clock` typing in the SDC
// and is captured by flops in two distinct clock domains (`clk_a`,
// `clk_b`) declared asynchronous. With CDC-011 absent today,
// find_crossings never walks the untyped port and the rule pack
// reports clean — locking that current behaviour in so any later
// change (CDC-011 landing, or a data-model change that retroactively
// walks untyped ports) is forced to update the paired test.
//
// When CDC-011 lands, flip the test assertion to expect one
// error-severity violation: `in` captured in {clk_a, clk_b}, which is
// intrinsically wrong regardless of SDC opinion (a single port cannot
// be synchronous to two distinct clocks).

module bad_unconstrained_input_two_domains (
    input  logic clk_a,
    input  logic clk_b,
    input  logic in,
    output logic q_a,
    output logic q_b
);

    always_ff @(posedge clk_a) q_a <= in;
    always_ff @(posedge clk_b) q_b <= in;

endmodule
