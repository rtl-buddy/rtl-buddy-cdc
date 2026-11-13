// Negative fixture: a req/ack bus handshake whose acknowledge returns
// through a third clock domain, with the last hop unsynchronised
// (rtl-buddy-cdc#331).
//
// Same three asynchronous clocks and structure as
// good_handshake_ack_via_third_domain: clk_a launches a held 8-bit
// payload and a level request, clk_b synchronises the request and loads
// the payload through a mux on that synchronised request, and clk_b's
// acknowledge is relayed through clk_c (2FF in, re-registered) back to
// clk_a. The clk_c → clk_a hop is captured by a single flop, a_ack_s1,
// which clk_a then uses to release the request: CDC-001 fires exactly
// once, on c_ack_q → a_ack_s1. The payload is still held until the
// (relayed) acknowledge, so CDC-012 must stay silent, and the gated
// payload is not a CDC-004.
//
//   clk_a [req_q, data_q] ──req 2FF──▶ clk_b [b_data ◀ mux(data_q)]
//        ▲                                   │ b_ack_q
//        └──1FF── clk_c [c_ack_q] ◀──2FF─────┘   → CDC-001 (C → A)

module bad_handshake_ack_via_third_domain (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       clk_c,
    input  logic       rst_n,
    input  logic       start,
    input  logic [7:0] d_in,
    output logic [7:0] q_data,
    output logic       busy
);

    // ---- clk_a: source, holds the payload until the relayed ack ----
    logic       req_q, a_ack_s1;
    logic [7:0] data_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) begin
            req_q  <= 1'b0;
            data_q <= 8'h00;
        end else if (!req_q && !a_ack_s1 && start) begin
            req_q  <= 1'b1;
            data_q <= d_in;
        end else if (req_q && a_ack_s1) begin
            req_q  <= 1'b0;
        end
    end
    assign busy = req_q | a_ack_s1;

    // ---- clk_b: request synchroniser, gated payload load, ack ----
    logic       b_req_s1, b_req_s2, b_ack_q;
    logic [7:0] b_data;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_req_s1 <= 1'b0;
            b_req_s2 <= 1'b0;
        end else begin
            b_req_s1 <= req_q;
            b_req_s2 <= b_req_s1;
        end
    end
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_ack_q <= 1'b0;
            b_data  <= 8'h00;
        end else begin
            b_ack_q <= b_req_s2;
            if (b_req_s2 && !b_ack_q) b_data <= data_q;
        end
    end
    assign q_data = b_data;

    // ---- clk_c: relays the acknowledge ----
    logic c_ack_s1, c_ack_s2, c_ack_q;
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) begin
            c_ack_s1 <= 1'b0;
            c_ack_s2 <= 1'b0;
        end else begin
            c_ack_s1 <= b_ack_q;
            c_ack_s2 <= c_ack_s1;
        end
    end
    always_ff @(posedge clk_c or negedge rst_n) begin
        if (!rst_n) c_ack_q <= 1'b0;
        else        c_ack_q <= c_ack_s2;
    end

    // ---- back into clk_a: BUG, a single capture flop ----
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_ack_s1 <= 1'b0;
        else        a_ack_s1 <= c_ack_q;
    end

endmodule
