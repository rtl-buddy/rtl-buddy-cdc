// Positive pair of bad_output_missing_contract (CDC-025, issue #329): the
// same design with every data output contracted. q_b and q_bus get their
// set_output_delay -clock, and the mixed-domain pair is split into one
// port per launch domain (q_pair_a, q_pair_b) so each can carry its own
// contract. The clock, constant and feed-through outputs need none.
// CDC-025 must stay silent, and so must every other rule.

module good_output_contracts_complete (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       d_a,
    input  logic       d_b,
    input  logic [3:0] bus_a,
    input  logic       en_b,
    output logic       q_a,
    output logic       q_b,
    output logic [3:0] q_bus,
    output logic       q_pair_a,
    output logic       q_pair_b,
    output logic       clk_fwd,
    output logic       clk_gated,
    output logic       div_o,
    output logic       tie_o,
    output logic       thru_o
);

    logic en_q;
    logic div_q;
    logic slow_q;
    logic gated_q;

    always_ff @(posedge clk_a) begin
        q_a      <= d_a;
        q_bus    <= bus_a;
        q_pair_a <= d_a;
        div_q    <= ~div_q;
    end

    always_ff @(posedge clk_b) begin
        q_b      <= d_b;
        q_pair_b <= d_b;
        en_q     <= en_b;
    end

    // Flops clocked by the divider and by the gated clock, so both drive
    // the clock network as well as an output.
    always_ff @(posedge div_q) slow_q <= d_a;
    always_ff @(posedge clk_gated) gated_q <= d_b;

    assign clk_fwd   = clk_a;
    assign clk_gated = clk_b & en_q;
    assign div_o     = div_q;
    assign tie_o     = 1'b0;
    assign thru_o    = d_a;

endmodule
