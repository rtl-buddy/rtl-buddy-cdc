// Negative-case fixture: typed top-level bus crossing.
//
// `in[7:0]` is typed to clk_a with set_input_delay, then sampled by a
// per-bit 2FF vector synchronizer in clk_b. The input typing prevents
// CDC-011, but it does not prove the bus is coherent when sampled in
// clk_b. CDC-004 must fire because there is no handshake/gating and no
// gray-code guarantee.

module bad_typed_port_bus_crossing (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic [7:0] in,
    output logic [7:0] q_a,
    output logic [7:0] q_b
);

    always_ff @(posedge clk_a) q_a <= in;

    logic [7:0] sync_meta;
    logic [7:0] sync_q;
    always_ff @(posedge clk_b) sync_meta <= in;
    always_ff @(posedge clk_b) sync_q    <= sync_meta;
    assign q_b = sync_q;

endmodule
