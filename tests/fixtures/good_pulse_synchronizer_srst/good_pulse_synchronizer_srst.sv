// Canonical pulse synchroniser whose flops carry a synchronous reset
// (rtl-buddy-cdc#376).
//
// The same toggle + 2FF + XOR-tail idiom as `good_pulse_synchronizer`,
// with `if (rst) ... <= 1'b0;` in place of the asynchronous reset.
// After `proc` every reset is a constant-leg `$mux` in front of the
// flop's `D`: the source toggle's `D` is `rst ? 0 : (event ? ~q : q)`,
// and the delayed copy `toggle_sync_d` reads `toggle_sync` through its
// own reset mux. A reset only forces a constant, so the toggle is still
// a toggle and the XOR tail is still the pulse-recovery tail. CDC-013
// must stay silent on the `proc` netlist and on the `.optdff.json`
// netlist (the same design after `opt_dff`, where the resets are folded
// into `$sdff` / `$sdffe` cells) alike.

module good_pulse_synchronizer_srst (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic src_rst,
    input  logic dst_rst,
    input  logic event_in,
    output logic event_seen
);

    logic src_toggle;
    always_ff @(posedge src_clk) begin
        if (src_rst)       src_toggle <= 1'b0;
        else if (event_in) src_toggle <= ~src_toggle;
    end

    logic toggle_meta;
    logic toggle_sync;
    logic toggle_sync_d;
    always_ff @(posedge dst_clk) begin
        if (dst_rst) begin
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
