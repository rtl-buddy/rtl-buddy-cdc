// Two full-width gated captures of one held payload (rtl-buddy-cdc#386).
//
// A req/ack handshake holds the 8-bit src_clk register `payload` from
// request until the acknowledge returns. dst_clk captures it twice,
// into `cap_a` and `cap_b`, each loading all eight bits under the same
// synchronised request `req_s2`. Each capture is a properly gated bus
// (CDC-004 silent, CDC-012 silent: the acknowledge closes the loop).
//
// CDC-020 used to read the two destinations of `payload` as two lanes
// of a sliced bus. Its lanes are crossings narrower than the source; a
// crossing that lands the whole bus on one register is a bus crossing,
// so nothing fires.

module good_gated_bus_double_capture (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       src_req,
    input  logic [7:0] src_data,
    output logic [7:0] cap_a,
    output logic [7:0] cap_b
);
    // --- src_clk: held request and payload, acknowledge synchroniser ---
    logic dst_ack, ack_meta, ack_sync, src_load_q;
    logic [7:0] payload;
    always_ff @(posedge src_clk) begin
        ack_meta <= dst_ack;
        ack_sync <= ack_meta;
        if (src_req)       src_load_q <= 1'b1;
        else if (ack_sync) src_load_q <= 1'b0;
        if (src_req && !src_load_q && !ack_sync) payload <= src_data;
    end

    // --- dst_clk: request synchroniser, two captures, acknowledge ---
    logic req_s1, req_s2;
    always_ff @(posedge dst_clk) begin
        req_s1  <= src_load_q;
        req_s2  <= req_s1;
        dst_ack <= req_s2;
        if (req_s2) cap_a <= payload;
        if (req_s2) cap_b <= payload;
    end
endmodule
