// Positive-case fixture: redundant synchronizers WITHOUT downstream
// recombination.
//
// A single source-domain flop fans out to two independent 2FF
// synchronizer chains in the destination domain — same shape as
// bad_reconvergent_sync.sv — BUT the two synchronized outputs drive
// disjoint downstream registers and disjoint output ports. There's
// no cell that observes both synchronized values together, so the
// mismatched-resolution failure mode the rule guards against
// cannot actually be triggered.
//
// Phase 2 of CDC-005 (issue #33) introduces a forward-cone
// reconvergence filter: it should classify this fixture as harmless
// and NOT fire, while keeping the bad_reconvergent_sync fixture
// firing.

module good_disjoint_fanout_sync_chains (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_a_out,
    output logic q_b_out
);

    logic src_q_a, src_q_b;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            src_q_a <= 1'b0;
            src_q_b <= 1'b0;
        end else begin
            src_q_a <= d_in;
            src_q_b <= d_in;
        end
    end

    // Two independent synchronizer chains, fed by independent source
    // registers. This keeps the fixture focused on the "disjoint
    // downstream cones" positive shape without tripping the
    // reconvergent multi-synchronizer check (CDC-005) on a single
    // source flop.
    logic sync_a_meta, sync_a_q;
    logic sync_b_meta, sync_b_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_meta <= 1'b0;
        else        sync_a_meta <= src_q_a;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_q <= 1'b0;
        else        sync_a_q <= sync_a_meta;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_meta <= 1'b0;
        else        sync_b_meta <= src_q_b;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_q <= 1'b0;
        else        sync_b_q <= sync_b_meta;
    end

    // Downstream registers — independent. Each chain feeds its own
    // register driving its own output. No cell observes both
    // synchronized values.
    logic out_a_reg, out_b_reg;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) out_a_reg <= 1'b0;
        else        out_a_reg <= sync_a_q;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) out_b_reg <= 1'b0;
        else        out_b_reg <= sync_b_q;
    end

    assign q_a_out = out_a_reg;
    assign q_b_out = out_b_reg;

endmodule
