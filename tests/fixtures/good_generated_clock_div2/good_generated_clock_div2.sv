// Positive fixture: a /2 clock divider declared via
// create_generated_clock. The downstream flop runs in clk_div2 (the
// divider's Q), and a flop in clk hands data to it. Because clk and
// clk_div2 share a master, are_async() must return false for the pair
// — no CDC violation should fire.

module good_generated_clock_div2 (
    input  logic clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    // /2 divider: a single flop toggling on every clk edge.
    logic clk_div2;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) clk_div2 <= 1'b0;
        else        clk_div2 <= ~clk_div2;
    end

    // Pre-register the top-level data input so the sync-chain check
    // sees a flop in the fanin (avoiding an unrelated CDC-006 hit on
    // d_in → src_q).
    (* cdc_sync *) logic d_in_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) d_in_q <= 1'b0;
        else        d_in_q <= d_in;
    end

    // Source flop in clk.
    logic src_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in_q;
    end

    // Destination flop in clk_div2 sampling the source.
    logic dst_q;
    always_ff @(posedge clk_div2 or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
