// Positive fixture: a req/ack bus handshake whose acknowledge returns
// through a third clock domain (rtl-buddy-cdc#331).
//
// clk_a, clk_b and clk_c are mutually asynchronous. clk_a launches an
// 8-bit payload and a level request; clk_b synchronises the request
// (2FF) and loads the payload through a mux whose select is that
// synchronised request, so the bus crossing is gated (no CDC-004).
// clk_b's acknowledge does not go straight back: it is synchronised into
// clk_c (2FF), re-registered there (a bridge that forwards it), and
// synchronised from clk_c into clk_a (2FF). clk_a holds data_q and req_q
// until that acknowledge arrives, so the payload is stable for the whole
// time clk_b may sample it, which is the guarantee CDC-012 asks for.
// Four async crossings (payload, request, ack B → C, ack C → A), all
// gated or 2FF-synchronised. Expected verdict: clean.
//
//   clk_a [req_q, data_q] ──req 2FF──▶ clk_b [b_data ◀ mux(data_q)]
//        ▲                                   │ b_ack_q
//        └──2FF── clk_c [c_ack_q] ◀──2FF─────┘

module good_handshake_ack_via_third_domain (
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
    logic       req_q, a_ack_s1, a_ack_s2;
    logic [7:0] data_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) begin
            req_q  <= 1'b0;
            data_q <= 8'h00;
        end else if (!req_q && !a_ack_s2 && start) begin
            req_q  <= 1'b1;
            data_q <= d_in;
        end else if (req_q && a_ack_s2) begin
            req_q  <= 1'b0;
        end
    end
    assign busy = req_q | a_ack_s2;

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

    // ---- back into clk_a ----
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) begin
            a_ack_s1 <= 1'b0;
            a_ack_s2 <= 1'b0;
        end else begin
            a_ack_s1 <= c_ack_q;
            a_ack_s2 <= a_ack_s1;
        end
    end

endmodule
