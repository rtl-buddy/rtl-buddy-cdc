// Negative-case fixture for CDC-016 (issue #175): an opposite-edge
// synchroniser. The first stage samples on the positive edge of
// `dst_clk`; the second stage on the negative edge of the same
// clock. Each metastable value has only half a clock period to
// resolve instead of a full period — MTBF is roughly halved.
//
// The RTL looks syntactically symmetric (2FF on `dst_clk`), and the
// structural depth check (CDC-001 / CDC-002) sees a valid chain at
// depth 2 — both rules stay silent. CDC-016 fires by walking the
// chain via `_sync_chain_flops` and noticing that adjacent stages
// disagree on CLK_POLARITY.

module bad_opposite_edge_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // Stage 1: positive edge of dst_clk.
    logic sync_1;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_1 <= 1'b0;
        else        sync_1 <= src_q;
    end

    // Stage 2: NEGATIVE edge of the same clock — the bug. Yosys
    // encodes the polarity in the `$adff.CLK_POLARITY` parameter.
    logic sync_2;
    always_ff @(negedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_2 <= 1'b0;
        else        sync_2 <= sync_1;
    end

    assign q_out = sync_2;

endmodule
