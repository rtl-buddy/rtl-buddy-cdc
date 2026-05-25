// Negative-case fixture for CDC-021.
//
// A top-level input port (clk_aux) drives a flop's CLK pin but has
// no create_clock declaration in the SDC. The analyzer silently
// accepts the port name as the flop's clock domain — but
// `are_async` returns False against every declared clock (no
// async-group entry), so `_filter_async` drops every crossing
// involving the undeclared domain and the rule pack stays silent
// on flops in that domain.
//
// CDC-021 must fire once on the (clk_aux, [q_aux]) pair. No other
// rule fires — there's no data port, no cross-domain crossing, and
// the toggle is self-contained.

module bad_flop_clk_undeclared_port (
    input  logic clk_aux,
    output logic q_out
);

    logic q_aux;
    always_ff @(posedge clk_aux) q_aux <= ~q_aux;

    assign q_out = q_aux;

endmodule
