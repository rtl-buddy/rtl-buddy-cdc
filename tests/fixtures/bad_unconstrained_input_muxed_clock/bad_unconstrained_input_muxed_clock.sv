// Negative-case fixture for CDC-011 (#97), muxed-clock destination.
//
// 4-bit input `d[3:0]` has no `set_input_delay -clock` typing and is
// captured by a flop whose clock is a test-mode mux between two
// declared clocks (`tm ? tclk : sclk`). The destination clock isn't a
// single `create_clock` name, so even a methodologically diligent SDC
// author couldn't default `d` to one of them. CDC-011 should fire as
// a warning (single resolved destination domain — whichever side of
// the mux trace_clock_root picks first).

module bad_unconstrained_input_muxed_clock (
    input  logic       tclk,
    input  logic       sclk,
    input  logic       tm,
    input  logic [3:0] d,
    output logic [3:0] q
);

    wire muxedCP = tm ? tclk : sclk;
    always_ff @(posedge muxedCP) q <= d;

endmodule
