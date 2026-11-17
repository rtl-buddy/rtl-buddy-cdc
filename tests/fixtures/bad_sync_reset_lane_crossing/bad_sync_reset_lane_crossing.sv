// A synchronous reset from another clock domain on some lanes of a
// register only (rtl-buddy-cdc#398).
//
// kill_a is a clk_a register. In clk_b, lanes 1:0 of q reset on
// kill_a, lanes 3:2 on the local port rb. `opt_dff` slices q into one
// `$sdff` per reset; `proc` leaves one `$dff` with both resets as
// constant-leg muxes on D. Either way the kill_a reset reaches clk_b
// unsynchronised: RDC-003 must fire once, naming kill_a, in both
// forms, and the reset-domain map must record the crossing on lanes 1:0
// only. The crossing also reaches q's D, so the CDC rules report it as
// well.

module bad_sync_reset_lane_crossing (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       kill_in,
    input  logic       rb,
    input  logic [3:0] d,
    output logic [3:0] q
);
    logic kill_a;
    always_ff @(posedge clk_a) kill_a <= kill_in;

    always_ff @(posedge clk_b) begin
        if (kill_a) q[1:0] <= '0;
        else        q[1:0] <= d[1:0];
        if (rb)     q[3:2] <= '0;
        else        q[3:2] <= d[3:2];
    end
endmodule
