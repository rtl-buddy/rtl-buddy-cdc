// Negative fixture for set_case_analysis on a clock-mux select.
//
// clk_m = sel ? clk_b : clk_a feeds the capture flop dst_q, which takes
// a raw single-flop crossing from src_q (clk_a). The SDC pins the mode
// port with `set_case_analysis 1 [get_ports sel]`, so in the constrained
// mode the mux always passes clk_b and dst_q is a clk_b flop. Before
// rtl-buddy-cdc#325 the tracer ignored the case analysis and took the
// mux's first resolving leg (clk_a), so dst_q looked same-domain and the
// crossing was never reported; resolving the select makes CDC-001 fire.

module bad_case_analysis_clock_mux (
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

    // Single capture flop on the muxed clock — no second stage.
    logic dst_q;
    always_ff @(posedge clk_m or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
