// Negative fixture for a port carrying two clocks (create_clock -add).
//
// Port clk_a carries two clocks: clk_a (functional mode, synchronous to
// clk_b) and clk_a2 (a second mode added with -add, asynchronous to
// clk_b). The two modes are physically exclusive. A raw flop→flop wire
// crosses clk_a → clk_b and another clk_b → clk_a, each with a single
// capture flop. In clk_a2 mode both are asynchronous crossings, so
// CDC-001 must fire on both, naming clk_a2 (rtl-buddy-cdc#330).
//
//      clk_a (+clk_a2)          clk_b
//   ┌──[ a_q ]────────────────[ b_cap ]
//   └──[ a_cap ]──────────────[ b_q ]──┘
//   single capture flop each way → CDC-001 ×2

module bad_multi_clock_port (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_b,
    output logic q_a,
    output logic q_b
);

    logic a_q, a_cap, b_q, b_cap;

    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end

    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= d_b;
    end

    // BAD: single capture flop on each side.
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) b_cap <= 1'b0;
        else        b_cap <= a_q;
    end

    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) a_cap <= 1'b0;
        else        a_cap <= b_q;
    end

    assign q_a = a_cap;
    assign q_b = b_cap;

endmodule
