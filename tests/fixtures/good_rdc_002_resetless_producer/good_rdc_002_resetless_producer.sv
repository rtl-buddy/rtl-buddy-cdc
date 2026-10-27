// RDC-002 must stay silent when the producer flop is resetless (#306).
//
// rst is a plain flop with no reset of its own (a $dff, so no
// ARST_VALUE). It drives the active-high async reset of rst_n, a local
// assert-async / deassert-sync stage. While rst is 1, rst_n is held in
// reset, which is the intended wiring. The producer has no reset value,
// so there is no polarity for the consumer to disagree with.
//
// rst_n then drives q's active-low reset, and rst_n resets to 0, so
// that second hop matches too.

module good_rdc_002_resetless_producer (
    input  logic clk,
    input  logic rst_i,
    input  logic d,
    output logic q
);

    logic rst;
    always_ff @(posedge clk) rst <= rst_i;

    logic rst_n;
    always_ff @(posedge clk or posedge rst)
        if (rst) rst_n <= 1'b0;
        else     rst_n <= 1'b1;

    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 1'b0;
        else        q <= d;

endmodule
