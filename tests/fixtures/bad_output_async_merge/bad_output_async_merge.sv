// Negative fixture for CDC-025 (issue #329): top-level outputs that
// combinationally merge launches from two clock domains the SDC declares
// asynchronous. No output carries a set_output_delay contract, but a
// boundary signal mixing asynchronous domains has no single clock any
// receiver can capture it on, so no constraint could make it correct.
//
// irq ORs a clk_a flag with a clk_b flag. status gates a clk_a bus with a
// clk_b enable, so all four bits merge both domains. Two controls must
// stay silent: mixed concatenates a clk_a pair with a clk_b pair (no bit
// merges them), and q_a is a plain clk_a register. Outputs launched in a
// single domain are only reported once the SDC types some output, and
// this one types none. CDC-025 must fire as an error once on irq and
// once on status, and nothing else may fire.

module bad_output_async_merge (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       d_a,
    input  logic       d_b,
    input  logic [3:0] bus_a,
    input  logic [1:0] pair_b,
    input  logic       en_b,
    output logic       irq,
    output logic [3:0] status,
    output logic [3:0] mixed,
    output logic       q_a
);

    logic       flag_a;
    logic       flag_b;
    logic [3:0] bus_a_q;
    logic [1:0] pair_b_q;
    logic       en_b_q;

    always_ff @(posedge clk_a) begin
        flag_a  <= d_a;
        bus_a_q <= bus_a;
        q_a     <= d_a;
    end

    always_ff @(posedge clk_b) begin
        flag_b   <= d_b;
        pair_b_q <= pair_b;
        en_b_q   <= en_b;
    end

    assign irq    = flag_a | flag_b;
    assign status = en_b_q ? bus_a_q : 4'h0;
    assign mixed  = {bus_a_q[3:2], pair_b_q};

endmodule
