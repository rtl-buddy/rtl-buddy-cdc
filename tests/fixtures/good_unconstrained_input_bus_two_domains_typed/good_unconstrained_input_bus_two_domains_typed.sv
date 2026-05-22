// Positive counterpart to bad_unconstrained_input_bus_two_domains.
//
// Bus variant of good_unconstrained_input_two_domains_typed — `in`
// and `load` are typed against clk_a; the clk_b capture is gated by
// a synchronised load bit. CDC-011 stays silent because the input
// is typed, and CDC-004 stays silent because the destination only
// samples the bus under a dst-domain synchronised enable.
//
// The fixture also implements a full req/ack handshake between the
// two clock domains so CDC-012 (functional data-hold) stays silent:
// `q_a` is loaded only when arming a new request, held stable
// across the round-trip, and the request clears only when a
// synced-back ack confirms clk_b has sampled. Without the ack
// feedback the source payload could change between the request
// being registered and clk_b latching it — the CDC-012 failure
// mode.

module good_unconstrained_input_bus_two_domains_typed (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       load,
    input  logic [7:0] in,
    output logic [7:0] q_a,
    output logic [7:0] q_b
);

    // Synced-back ack: clk_a learns when clk_b has sampled.
    logic clk_b_ack;
    logic ack_meta, ack_sync;
    always_ff @(posedge clk_a) begin
        ack_meta <= clk_b_ack;
        ack_sync <= ack_meta;
    end

    // Held request: set on `load`, clear on synced-back ack.
    logic load_q;
    always_ff @(posedge clk_a) begin
        if (load)          load_q <= 1'b1;
        else if (ack_sync) load_q <= 1'b0;
    end

    // Held payload: load only when arming a new request and no ack
    // is pending. Holds stable across the round-trip.
    always_ff @(posedge clk_a) begin
        if (load && !load_q && !ack_sync) q_a <= in;
    end

    // 2FF synchroniser of the load request into clk_b; ack on the
    // synced enable.
    logic load_meta, load_sync;
    always_ff @(posedge clk_b) begin
        load_meta <= load_q;
        load_sync <= load_meta;
        clk_b_ack <= load_sync;
    end

    always_ff @(posedge clk_b) begin
        if (load_sync) q_b <= q_a;
    end

endmodule
