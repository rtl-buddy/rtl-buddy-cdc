// Negative-case fixture for RDC-008.
//
// A top-level reset port (raw_rst_n) drives flops in two clock
// domains. clk_a has a proper 2FF reset-synchroniser chain — the
// user clearly knows the port needs synchronisation. clk_b uses
// the raw port directly on ≥2 flops without any chain — the
// "forgot to add the chain in clk_b" methodology bug RDC-008
// catches. Reset assertion is fine (combinational), but the
// deassertion edge is unsynchronised to clk_b — recovery/removal
// timing violations can leave the clk_b flops in different reset
// states.
//
// RDC-001 stays silent because the reset source is a port (not a
// foreign-domain flop). RDC-008 must fire once on the (raw_rst_n,
// clk_b) group.

module bad_primary_reset_unsynced (
    input  logic clk_a,
    input  logic clk_b,
    input  logic raw_rst_n,
    input  logic d_in_a,
    input  logic d_in_b,
    output logic q_a,
    output logic q_b
);

    // 2FF reset-sync chain in clk_a — intent is clear.
    logic rst_a_meta, rst_a_n;
    always_ff @(posedge clk_a or negedge raw_rst_n)
        if (!raw_rst_n) rst_a_meta <= 1'b0;
        else            rst_a_meta <= 1'b1;
    always_ff @(posedge clk_a or negedge raw_rst_n)
        if (!raw_rst_n) rst_a_n    <= 1'b0;
        else            rst_a_n    <= rst_a_meta;

    // clk_a consumer routes through the chain — silent.
    logic qa_q;
    always_ff @(posedge clk_a or negedge rst_a_n)
        if (!rst_a_n) qa_q <= 1'b0;
        else          qa_q <= d_in_a;
    assign q_a = qa_q;

    // BAD: clk_b consumers use raw_rst_n directly, no chain in clk_b.
    logic qb_q0, qb_q1;
    always_ff @(posedge clk_b or negedge raw_rst_n) begin
        if (!raw_rst_n) begin
            qb_q0 <= 1'b0;
            qb_q1 <= 1'b0;
        end else begin
            qb_q0 <= d_in_b;
            qb_q1 <= qb_q0;
        end
    end
    assign q_b = qb_q1;

endmodule
