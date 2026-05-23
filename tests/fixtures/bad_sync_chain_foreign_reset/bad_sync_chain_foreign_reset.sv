// Negative-case fixture for CDC-015 (issue #172): a 2FF sync chain
// in `dst_clk` whose ARST is driven by a reset signal *registered
// in src_clk*. The chain's resolving flops are released
// asynchronously to dst_clk every time the foreign reset deasserts,
// so the chain cannot reach steady state on its own clock — exactly
// the synchroniser-reset-asynchrony failure mode CDC-015 targets.
//
// CDC-001 / CDC-002 see a structurally valid 2FF chain at depth 2
// and stay silent — the failure is in the chain's reset path, not
// its data path. RDC-001 fires independently on each sync flop's
// ARST (foreign-domain reset source). The two findings coexist;
// CDC-015's framing tells the user the fix is "use dst_clk's reset
// for the chain", not "add a reset synchroniser to the reset path".

module bad_sync_chain_foreign_reset (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,

    input  logic async_signal,
    output logic dst_q
);

    // Reset signal registered in src_clk. In a real design this
    // would be the output of a reset synchroniser on src_clk; here
    // we simplify to a 1-bit flop that holds the reset state.
    logic src_rst_q;
    always_ff @(posedge src_clk or negedge global_rst_n) begin
        if (!global_rst_n) src_rst_q <= 1'b0;
        else               src_rst_q <= 1'b1;
    end

    // Source-domain data flop — produces the cross-domain signal.
    logic src_q;
    always_ff @(posedge src_clk or negedge global_rst_n) begin
        if (!global_rst_n) src_q <= 1'b0;
        else               src_q <= async_signal;
    end

    // dst_clk sync chain. RESET WRONG: both stages take their ARST
    // from src_rst_q (an src_clk flop), not from a dst_clk-domain
    // reset. The chain cannot reach steady state.
    logic sync_1;
    always_ff @(posedge dst_clk or negedge src_rst_q) begin
        if (!src_rst_q) sync_1 <= 1'b0;
        else            sync_1 <= src_q;
    end

    logic sync_2;
    always_ff @(posedge dst_clk or negedge src_rst_q) begin
        if (!src_rst_q) sync_2 <= 1'b0;
        else            sync_2 <= sync_1;
    end

    assign dst_q = sync_2;

endmodule
