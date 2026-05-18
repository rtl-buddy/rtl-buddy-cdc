// Positive-case fixture for RDC-002's port-declared-polarity variant.
//
// Same shape as `bad_marked_reset_polarity` but the flop is wired
// `negedge rst_n`, agreeing with the port's
// `(* reset_polarity = "low" *)` declaration. The analyzer sees:
//
//   port-declared polarity  = low  (asserts on '0')
//   flop ARST_POLARITY       = 0   (asserts on '0')
//
// — they match, so no RDC-002 violation. This pairs with the bad
// fixture as the regression net for the new check.

module good_marked_reset_polarity (
    input  logic clk,
    (* reset_polarity = "low" *) input logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic good_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) good_q <= 1'b0;
        else        good_q <= d_in;
    end

    assign q_out = good_q;

endmodule
