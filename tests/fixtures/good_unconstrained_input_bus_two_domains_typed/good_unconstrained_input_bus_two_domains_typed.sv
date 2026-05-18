// Positive counterpart to bad_unconstrained_input_bus_two_domains.
//
// Bus variant of good_unconstrained_input_two_domains_typed — `in`
// is typed against clk_a; the clk_b capture passes through a
// 2-stage flop chain per bit. CDC-011 and CDC-001/-004 all stay
// silent.

module good_unconstrained_input_bus_two_domains_typed (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic [7:0] in,
    output logic [7:0] q_a,
    output logic [7:0] q_b
);

    always_ff @(posedge clk_a) q_a <= in;

    (* cdc_sync *) logic [7:0] sync_meta;
    logic [7:0]                sync_q;
    always_ff @(posedge clk_b) sync_meta <= in;
    always_ff @(posedge clk_b) sync_q    <= sync_meta;
    assign q_b = sync_q;

endmodule
