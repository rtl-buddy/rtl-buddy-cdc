// Negative-case fixture: a clock signal is wired into a data input
// pin instead of a clock pin. Clocks live on dedicated low-skew
// networks; sampling one as data delivers the high-frequency edge
// toggle, breaks STA, and is almost always a wiring mistake. Should
// trip CDC-008.

module bad_clock_as_data (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_n,
    output logic clk_a_seen,
    output logic anded
);

    // BAD: clk_a is sampled by a clk_b-domain flop — used as DATA.
    logic q;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= clk_a;
    end
    assign clk_a_seen = q;

    // BAD: combinational AND of two clock signals — clocks must not
    // drive logic gates as data.
    assign anded = clk_a & clk_b;

endmodule
