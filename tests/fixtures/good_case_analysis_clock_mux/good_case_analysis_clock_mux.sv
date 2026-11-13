// Positive fixture for set_case_analysis on a clock-mux select.
//
// Same clock mux as bad_case_analysis_clock_mux — clk_m = sel ? clk_b :
// clk_a — with `set_case_analysis 1 [get_ports sel]`, so the capture
// side is a clk_b domain in the constrained mode (rtl-buddy-cdc#325).
// The textbook fix is in place: src_q crosses into a 2FF synchronizer
// clocked by the muxed clock, so the crossing is reported as async and
// no rule fires.

module good_case_analysis_clock_mux (
    input  logic clk_a,
    input  logic clk_b,
    input  logic sel,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic clk_m;
    assign clk_m = sel ? clk_b : clk_a;

    logic src_q;
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // 2FF synchronizer on the muxed clock.
    (* cdc_sync *) logic sync_meta;
    always_ff @(posedge clk_m or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_q;
    end

    logic sync_q;
    always_ff @(posedge clk_m or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
