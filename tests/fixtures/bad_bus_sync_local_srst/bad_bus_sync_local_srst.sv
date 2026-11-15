// Binary (not gray) counter into a multi-bit 2FF synchroniser whose
// stages carry a synchronous reset from a dst_clk register
// (rtl-buddy-cdc#376).
//
// After `proc` the reset is a constant-leg `$mux` in front of each
// stage's `D`, selected by a dst_clk flop. That is the same shape as a
// load-enable mux selected by a synchronised valid, but a reset only
// forces the lanes to zero: between resets the first stage samples the
// bus on every edge, so several bits can settle on different cycles and
// the captured value can be one the source never held. CDC-004 must
// fire, on the `proc` netlist and on the `.optdff.json` netlist (the
// same design after `opt_dff`, where each reset is a `$sdff`) alike.
//
// Paired with `good_gray_bus_sync_srst` (gray source, same stages).

module bad_bus_sync_local_srst (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_a,
    input  logic        rst_b,
    input  logic        incr,
    output logic [3:0]  q
);

    logic [3:0] bin;
    always_ff @(posedge src_clk) begin
        if (rst_a) bin <= 4'd0;
        else       bin <= bin + 4'(incr);
    end

    // Reset synchronised into dst_clk.
    logic rst_s0, rst_s1;
    always_ff @(posedge dst_clk) begin
        rst_s0 <= rst_b;
        rst_s1 <= rst_s0;
    end

    logic [3:0] meta;
    always_ff @(posedge dst_clk) begin
        meta <= bin;
        q    <= meta;
        if (rst_s1) begin
            meta <= 4'd0;
            q    <= 4'd0;
        end
    end

endmodule
