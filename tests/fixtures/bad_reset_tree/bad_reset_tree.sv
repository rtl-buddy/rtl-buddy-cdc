// Negative-case fixture: a single async-reset source flop in src_clk
// fans out to FOUR flops' ARST pins in dst_clk, with no reset
// synchronizer in between. The grouped CDC-007 should fire ONCE for
// this shared source (not four times) and list all four destinations
// in the message — matching how a reset distribution tree is
// reviewed in practice.

module bad_reset_tree (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic src_rst_n,
    input  logic dst_rst_n,
    input  logic ext_rst_assert,
    output logic [3:0] q_out
);

    // Reset source flop in src_clk. Its Q is used as an async-reset
    // for downstream flops in dst_clk — that's the CDC-007 anti-
    // pattern (no sync-deassert in dst_clk).
    logic foreign_rst;
    always_ff @(posedge src_clk or negedge src_rst_n) begin
        if (!src_rst_n) foreign_rst <= 1'b1;
        else            foreign_rst <= ext_rst_assert;
    end

    logic [3:0] dst_q;
    always_ff @(posedge dst_clk or negedge foreign_rst) begin
        if (!foreign_rst) dst_q[0] <= 1'b0;
        else              dst_q[0] <= dst_q[3];
    end
    always_ff @(posedge dst_clk or negedge foreign_rst) begin
        if (!foreign_rst) dst_q[1] <= 1'b0;
        else              dst_q[1] <= dst_q[0];
    end
    always_ff @(posedge dst_clk or negedge foreign_rst) begin
        if (!foreign_rst) dst_q[2] <= 1'b0;
        else              dst_q[2] <= dst_q[1];
    end
    always_ff @(posedge dst_clk or negedge foreign_rst) begin
        if (!foreign_rst) dst_q[3] <= 1'b0;
        else              dst_q[3] <= dst_q[2];
    end

    // Suppress unused warning for dst_rst_n.
    /* verilator lint_off UNUSED */
    wire _unused = dst_rst_n;
    /* verilator lint_on UNUSED */

    assign q_out = dst_q;

endmodule
