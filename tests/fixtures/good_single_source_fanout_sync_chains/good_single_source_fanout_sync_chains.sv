// Single source flop fanning out to two independent sync chains.
//
// Same shape as `bad_reconvergent_sync` — one src_q feeds two
// 2FF synchronizer chains in dst_clk — BUT the two synchronized
// outputs drive disjoint downstream registers and disjoint output
// ports. There is no cell observing both synchronized values, so
// CDC-005's forward-cone reconvergence filter must NOT fire.
//
// Distinct from `good_disjoint_fanout_sync_chains` (which uses two
// independent source flops): this fixture keeps the "single source,
// multiple chains" topology specifically. CDC-005's single-source
// fan-out detection has to walk the shared src_q through both chains
// and confirm the downstream cones are disjoint — testing that the
// shared-source path resolves cleanly.
//
// Paired with `bad_reconvergent_sync` (must fire) and with
// `good_disjoint_fanout_sync_chains` (two-source variant).

module good_single_source_fanout_sync_chains (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_a_out,
    output logic q_b_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // Two independent synchronizer chains, both fed from src_q.
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
