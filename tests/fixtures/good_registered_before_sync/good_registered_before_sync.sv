// Positive counterpart to bad_comb_before_sync.
//
// The bad version routes `src_q1 & src_q2` (combinational) directly
// into the synchronizer's first stage — the AND can glitch when
// either source flop transitions, so the sync samples a transient
// value. Fix: register the AND output in the source domain so the
// signal entering the sync chain is a clean flop output. CDC-003
// stays silent because min_hops between src_anded.Q and sync_meta.D
// is now 0 (direct flop-to-flop).

module good_registered_before_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic a,
    input  logic b,
    output logic q_out
);

    // Sample inputs on src_clk.
    logic src_a;
    logic src_b;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            src_a <= 1'b0;
            src_b <= 1'b0;
        end else begin
            src_a <= a;
            src_b <= b;
        end
    end

    // Combine and **register** the result in src_clk *before* the
    // crossing — this is the key step. Glitches still occur on
    // (src_a & src_b), but they're filtered by the src_anded flop's
    // setup/hold window, not propagated across domains.
    logic src_anded;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_anded <= 1'b0;
        else        src_anded <= src_a & src_b;
    end

    // Standard 2FF synchronizer in dst_clk.
    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_anded;
    end

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
