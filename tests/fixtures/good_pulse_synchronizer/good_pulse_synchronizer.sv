// Positive fixture: canonical pulse synchroniser for fast-to-slow
// event passing.
//
// Source-side toggle flop converts edges to level changes; the
// destination's 2FF chain synchronises the toggle; an XOR of the
// chain tail with its 1-cycle-delayed copy (toggle_sync ^
// toggle_sync_d) recovers one pulse per event in the destination
// clock domain. This is the textbook correct idiom (Cummings SNUG
// 2008 §6) — no event loss for events spaced more than ~2 dst
// cycles apart.
//
// CDC-013 must stay silent here: the source-side toggle pattern
// matches the rule's classifier, but the dst-side XOR-tail proves
// the chain reconstructs the pulse correctly. The XOR-tail
// recognition was added in rtl-buddy-cdc#196 — before that, this
// fixture false-fired CDC-013.

module good_pulse_synchronizer (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic event_in,
    output logic event_seen
);

    logic src_toggle;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)       src_toggle <= 1'b0;
        else if (event_in) src_toggle <= ~src_toggle;
    end

    logic toggle_meta;
    logic toggle_sync;
    logic toggle_sync_d;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            toggle_meta   <= 1'b0;
            toggle_sync   <= 1'b0;
            toggle_sync_d <= 1'b0;
            event_seen    <= 1'b0;
        end else begin
            toggle_meta   <= src_toggle;
            toggle_sync   <= toggle_meta;
            toggle_sync_d <= toggle_sync;
            event_seen    <= toggle_sync ^ toggle_sync_d;
        end
    end

endmodule
