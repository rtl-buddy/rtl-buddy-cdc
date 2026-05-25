// Negative-case fixture for CDC-020.
//
// A true 4-bit register whose bus is sliced into 1-bit lanes before
// crossing, with each lane independently 2FF-synced in the
// destination and recombined back into a 4-bit bus. CDC-004 misses
// this shape because each per-lane crossing's width is 1 — the
// multi-bit detector's `width <= 1` skip drops every lane even
// though the source is genuinely multi-bit.
//
// CDC-020 must fire once on the (src_flop, dst_clock) group. Sister
// of CDC-019 (shared comb decoder); the failure physics are
// identical but the source is a true multi-bit register here.

module bad_sliced_bus_reconvergence (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic [3:0] d_in,
    output logic [3:0] data_dst
);

    logic [3:0] data;
    always_ff @(posedge src_clk) data <= d_in;

    logic d0, d1, d2, d3;
    assign d0 = data[0];
    assign d1 = data[1];
    assign d2 = data[2];
    assign d3 = data[3];

    logic d0_m, d0_s, d1_m, d1_s, d2_m, d2_s, d3_m, d3_s;
    always_ff @(posedge dst_clk) begin
        d0_m <= d0; d0_s <= d0_m;
        d1_m <= d1; d1_s <= d1_m;
        d2_m <= d2; d2_s <= d2_m;
        d3_m <= d3; d3_s <= d3_m;
    end

    assign data_dst = {d3_s, d2_s, d1_s, d0_s};

endmodule
