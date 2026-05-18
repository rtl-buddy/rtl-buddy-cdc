// Positive counterpart to bad_unconstrained_input_muxed_clock.
//
// SDC types `d` against the same clock the muxed-clock flop resolves
// to. CDC-011 stays silent because the port is typed; CDC-001 stays
// silent because the source/destination domains match.

module good_unconstrained_input_muxed_clock_typed (
    input  logic       tclk,
    input  logic       sclk,
    input  logic       tm,
    input  logic [3:0] d,
    output logic [3:0] q
);

    wire muxedCP = tm ? tclk : sclk;
    always_ff @(posedge muxedCP) q <= d;

endmodule
