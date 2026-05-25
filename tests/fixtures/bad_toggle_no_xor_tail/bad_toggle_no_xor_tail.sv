// Negative fixture for CDC-013 — toggle synchroniser WITHOUT the
// XOR-tail pulse-reconstruction stage.
//
// Source-side toggle flop encodes events as level changes. The
// destination synchronises the toggle through a 2FF chain but uses
// the chain tail's Q directly as the output without XOR-ing it
// with a 1-cycle-delayed copy. The output therefore tracks the
// toggle level rather than emitting one pulse per event — two
// closely-spaced events that toggle the source twice between two
// destination samples become zero events at the destination.
//
// CDC-013 must fire on this shape; the XOR-tail suppression
// (added in rtl-buddy-cdc#196) only kicks in when the dst-side
// reconstructs the pulse properly. This fixture is the
// regression sentinel for "rule still catches the actual failure
// after the XOR-tail false-positive was suppressed".

module bad_toggle_no_xor_tail (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic event_in,
    output logic event_seen
);

    logic src_toggle;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)        src_toggle <= 1'b0;
        else if (event_in) src_toggle <= ~src_toggle;
    end

    logic toggle_meta;
    logic toggle_sync;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            toggle_meta <= 1'b0;
            toggle_sync <= 1'b0;
        end else begin
            toggle_meta <= src_toggle;
            toggle_sync <= toggle_meta;
        end
    end

    // No XOR-tail — the chain tail's Q is exposed directly.
    assign event_seen = toggle_sync;

endmodule
