// Negative-case fixture: two synchronizer chains whose Qs recombine
// in a downstream register via an OR reduction.
//
// Stronger version of bad_reconvergent_sync.sv — instead of driving
// an unregistered output port, the two synchronized values are
// combined and registered. Phase 2 of CDC-005 (issue #33)'s
// reconvergence filter must still fire on this shape: a flop that
// observes both synchronized values is a textbook recombination
// point, with the same mismatched-resolution failure mode the rule
// catches.

module bad_reconvergent_with_recombine (
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

    // Two independent 2FF synchronizer chains, both fed from src_q.
    logic sync_a_meta, sync_a_q;
    logic sync_b_meta, sync_b_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_meta <= 1'b0;
        else        sync_a_meta <= src_q;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_q <= 1'b0;
        else        sync_a_q <= sync_a_meta;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_meta <= 1'b0;
        else        sync_b_meta <= src_q;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_q <= 1'b0;
        else        sync_b_q <= sync_b_meta;
    end

    // Downstream register that observes BOTH synchronized values via
    // an OR reduction — the recombination point.
    logic combine_reg;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) combine_reg <= 1'b0;
        else        combine_reg <= sync_a_q | sync_b_q;
    end

    assign q_out = combine_reg;

endmodule
