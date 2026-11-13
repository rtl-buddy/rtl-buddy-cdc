// Positive fixture for a port carrying two clocks (create_clock -add).
//
// Same clocks as bad_multi_clock_port: port clk_a carries clk_a
// (synchronous to clk_b) and clk_a2 (added with -add, asynchronous to
// clk_b). Each crossing now lands in a 2FF synchroniser, so the clk_a2
// reading of both crossings is safe and no rule fires
// (rtl-buddy-cdc#330).
//
//      clk_a (+clk_a2)                clk_b
//   ┌──[ a_q ]────────────────[ b_s1 ]─[ b_s2 ]
//   └──[ a_s2 ]─[ a_s1 ]──────────────[ b_q ]──┘
//   2FF synchroniser each way → clean

module good_multi_clock_port (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_b,
    output logic q_a,
    output logic q_b
);

    logic a_q, a_s1, a_s2, b_q, b_s1, b_s2;

    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end

    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= d_b;
    end

    // GOOD: 2FF synchroniser into clk_b.
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            b_s1 <= 1'b0;
            b_s2 <= 1'b0;
        end else begin
            b_s1 <= a_q;
            b_s2 <= b_s1;
        end
    end

    // GOOD: 2FF synchroniser into clk_a.
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) begin
            a_s1 <= 1'b0;
            a_s2 <= 1'b0;
        end else begin
            a_s1 <= b_q;
            a_s2 <= a_s1;
        end
    end

    assign q_a = a_s2;
    assign q_b = b_s2;

endmodule
