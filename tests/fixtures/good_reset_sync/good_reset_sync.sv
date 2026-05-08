// Positive counterpart to bad_reset_crossing.
//
// The bad version uses a src_clk-domain flop's Q as the async reset
// of a dst_clk flop — the dst flop's recovery edge is unrelated to
// dst_clk, risking metastability on de-assertion. Fix: a dedicated
// "reset synchronizer" — a 2FF chain in dst_clk whose ARST input is
// the *primary* (top-level) async reset, providing async-assert /
// sync-deassert semantics. The data-path flops then use the
// synchronizer's output as their ARST.
//
// CDC-007 stays silent: each flop's ARST is either a top-level port
// (`raw_rst_n` for the synchronizer's own flops) or a same-domain
// flop (`dst_rst_n_sync` for the data-path flops).

module good_reset_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic raw_rst_n,
    input  logic d_in,
    output logic q_out
);

    // ---- reset synchronizer ----------------------------------------
    // Async-assert (raw_rst_n falling => meta clears immediately).
    // Sync-deassert (raw_rst_n rising => meta loads 1, q_sync follows
    // one dst_clk cycle later, so the deasserting edge is aligned to
    // dst_clk).
    logic dst_rst_meta;
    always_ff @(posedge dst_clk or negedge raw_rst_n) begin
        if (!raw_rst_n) dst_rst_meta <= 1'b0;
        else            dst_rst_meta <= 1'b1;
    end

    logic dst_rst_n_sync;
    always_ff @(posedge dst_clk or negedge raw_rst_n) begin
        if (!raw_rst_n) dst_rst_n_sync <= 1'b0;
        else            dst_rst_n_sync <= dst_rst_meta;
    end

    // ---- src-domain registered input -------------------------------
    logic src_q;
    always_ff @(posedge src_clk or negedge raw_rst_n) begin
        if (!raw_rst_n) src_q <= 1'b0;
        else            src_q <= d_in;
    end

    // ---- dst-domain data-path flops use the **synchronized** reset --
    logic sync_meta;
    always_ff @(posedge dst_clk or negedge dst_rst_n_sync) begin
        if (!dst_rst_n_sync) sync_meta <= 1'b0;
        else                 sync_meta <= src_q;
    end

    logic sync_q;
    always_ff @(posedge dst_clk or negedge dst_rst_n_sync) begin
        if (!dst_rst_n_sync) sync_q <= 1'b0;
        else                 sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
