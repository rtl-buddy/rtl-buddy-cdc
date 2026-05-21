// Positive counterpart to bad_unconstrained_input_muxed_clock.
//
// SDC types `d` against sclk, the same clock that captures it.
// CDC-011 stays silent because the port is typed; CDC-001/004 stay
// silent because there is no cross-domain data capture.

module good_unconstrained_input_muxed_clock_typed (
    input  logic       tclk,
    input  logic       sclk,
    input  logic       tm,
    input  logic       d,
    output logic       q
);

    always_ff @(posedge sclk) q <= d;

endmodule
