// Negative-case fixture for CDC-014 (issue #171): combinational
// logic *between* synchroniser stages.
//
// The chain looks like a 2FF synchroniser at a glance, but stage 1's
// Q is gated by a comb cell before reaching stage 2's D:
//
//   src_q → sync_1 → (sync_1 & mask_q) → sync_2
//
// The gate is the bug. stage_1 (`sync_1`) may still be metastable
// when the gate output is sampled by stage_2 on the next dst_clk
// edge, so the gate output can be a transient mix. The full clock
// period of resolution time the user thinks they bought is destroyed
// by the gate's propagation delay budget.
//
// CDC-001 / CDC-002 see depth=1 (the chain walker terminates because
// sync_1's Q drives a comb cell, not a flop's D directly) — but a
// follow-on flop *does* exist behind the gate, so reporting
// "no second-stage synchronizer" would mislead the user. CDC-001
// defers via `_chain_has_inter_stage_comb`, and CDC-014 fires with
// the correct framing.

module bad_comb_between_sync_stages (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    input  logic mask_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // Same-domain mask register (in dst_clk). The user thinks of
    // this as a constant under operating conditions, but a gate in
    // the chain is still a metastability hazard regardless of what
    // the gate's other input is doing.
    logic mask_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) mask_q <= 1'b1;
        else        mask_q <= mask_in;
    end

    // Synchroniser stage 1 — first dst-domain flop after the
    // crossing.
    logic sync_1;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_1 <= 1'b0;
        else        sync_1 <= src_q;
    end

    // THE BUG: AND gate between sync_1 and sync_2. The chain walker
    // can't extend past the gate, so depth=1, but the second stage
    // is structurally present.
    logic sync_2;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_2 <= 1'b0;
        else        sync_2 <= sync_1 & mask_q;
    end

    assign q_out = sync_2;

endmodule
