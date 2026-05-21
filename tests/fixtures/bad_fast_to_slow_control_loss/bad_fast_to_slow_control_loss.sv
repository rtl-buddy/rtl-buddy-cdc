// Negative fixture for fast-to-slow control-event loss.
//
// A fast source-domain event toggles a level that is sampled by a
// conventional 2FF synchronizer in a slower destination domain. If two
// events occur between destination samples, the toggle returns to its
// prior value and the destination loses both events. This is not a
// metastability failure; it is an event-accounting/protocol failure.

module bad_fast_to_slow_control_loss (
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
