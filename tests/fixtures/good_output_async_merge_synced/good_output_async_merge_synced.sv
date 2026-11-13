// Positive pair of bad_output_async_merge (CDC-025, issue #329): the same
// outputs, each now launched from a single domain. The clk_a flag is
// 2FF-synchronised into clk_b before irq ORs it with the clk_b flag, and
// the clk_b enable is 2FF-synchronised into clk_a before it gates the
// clk_a bus onto status. mixed (a clk_a pair next to a clk_b pair, no bit
// merging them) and q_a are unchanged. No output carries a
// set_output_delay contract, so single-domain outputs are not reported:
// CDC-025 must stay silent, and so must every other rule.

module good_output_async_merge_synced (
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
    logic       flag_a_s1;
    logic       flag_a_s2;
    logic       en_s1;
    logic       en_s2;

    always_ff @(posedge clk_a) begin
        flag_a  <= d_a;
        bus_a_q <= bus_a;
        q_a     <= d_a;
        en_s1   <= en_b_q;
        en_s2   <= en_s1;
    end

    always_ff @(posedge clk_b) begin
        flag_b    <= d_b;
        pair_b_q  <= pair_b;
        en_b_q    <= en_b;
        flag_a_s1 <= flag_a;
        flag_a_s2 <= flag_a_s1;
    end

    assign irq    = flag_a_s2 | flag_b;
    assign status = en_s2 ? bus_a_q : 4'h0;
    assign mixed  = {bus_a_q[3:2], pair_b_q};

endmodule
