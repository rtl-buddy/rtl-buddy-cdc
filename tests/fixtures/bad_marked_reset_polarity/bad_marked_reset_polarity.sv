// Negative-case fixture for RDC-002's port-declared-polarity variant
// (issue #107).
//
// The top-level port `rst_n` is annotated with
// `(* reset_polarity = "low" *)` — the designer is asserting this is
// an active-low signal. The flop `bad_q`, however, is wired
// `posedge rst_n`, so Yosys infers ARST_POLARITY=1 (active-high
// reset). The two disagree:
//
//   port-declared polarity  = low  (asserts on '0')
//   flop ARST_POLARITY       = 1   (asserts on '1')
//
// The flop will reset on the rising edge of rst_n — the moment the
// rest of the design comes out of reset. This is the classic
// "posedge on the active-low port" wiring bug.
//
// Without the port attribute the analyzer has no way to flag this
// (the flop is wired directly to a port, no producer flop to compare
// against, no clock-domain crossing). The attribute is what
// distinguishes "designer mistake" from "designer intent".

(* reset_polarity_test *) // dummy attr on module — harmless, ensures
                          // the parser tolerates other reset_*-ish
                          // unrelated attributes.
module bad_marked_reset_polarity (
    input  logic clk,
    (* reset_polarity = "low" *) input logic rst_n,
    input  logic d_in,
    output logic q_out
);

    // Wired posedge: Yosys infers ARST_POLARITY=1. Disagrees with the
    // port-declared "low" polarity. RDC-002 (port variant) fires.
    logic bad_q;
    always_ff @(posedge clk or posedge rst_n) begin
        if (rst_n) bad_q <= 1'b0;
        else       bad_q <= d_in;
    end

    assign q_out = bad_q;

endmodule
