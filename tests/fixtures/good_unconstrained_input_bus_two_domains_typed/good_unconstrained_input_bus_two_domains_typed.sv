// Positive counterpart to bad_unconstrained_input_bus_two_domains.
//
// Bus variant of good_unconstrained_input_two_domains_typed — `in`
// and `load` are typed against clk_a; the clk_b capture is gated by
// a synchronized load bit. CDC-011 stays silent because the input is
// typed, and CDC-004 stays silent because the destination only
// samples the bus under a dst-domain synchronized enable.

module good_unconstrained_input_bus_two_domains_typed (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       load,
    input  logic [7:0] in,
    output logic [7:0] q_a,
    output logic [7:0] q_b
);

    logic load_q;
    always_ff @(posedge clk_a) begin
        q_a    <= in;
        load_q <= load;
    end

    logic load_meta, load_sync;
    always_ff @(posedge clk_b) begin
        load_meta <= load_q;
        load_sync <= load_meta;
    end

    always_ff @(posedge clk_b) begin
        if (load_sync) q_b <= q_a;
    end

endmodule
