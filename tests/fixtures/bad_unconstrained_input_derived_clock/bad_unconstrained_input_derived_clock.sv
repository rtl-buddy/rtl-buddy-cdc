// Negative-case fixture for CDC-011 (#97).
//
// Input `in` has no `set_input_delay -clock` typing and is captured
// by a flop whose clock is a combinational function of three declared
// clocks (`clk_a & clk_b & clk_c`). There's no single declared domain
// the port could "obviously" belong to — the SDC author has to assert
// one explicitly. CDC-011 should fire as a warning (single resolved
// destination domain — whichever one trace_clock_root picks first
// out of the AND tree).

module bad_unconstrained_input_derived_clock (
    input  logic clk_a,
    input  logic clk_b,
    input  logic clk_c,
    input  logic in,
    output logic q
);

    wire derived = clk_a & clk_b & clk_c;
    always_ff @(posedge derived) q <= in;

endmodule
