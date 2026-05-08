// Positive fixture for set_input_delay -clock <my_clk> port-typing.
//
// A 2FF synchronizer in dst_clk has its D pin driven by combinational
// logic of two top-level inputs. Without an SDC declaration this
// would fire CDC-006 (the synchronizer is sampling a comb output of
// unregistered inputs). But the SDC declares both inputs as being in
// dst_clk's domain via set_input_delay, asserting they're already
// synchronous to dst_clk — so CDC-006 must not fire.

module good_input_delay_domain (
    input  logic dst_clk,
    input  logic rst_n,
    input  logic a,
    input  logic b,
    output logic q_out
);

    logic sync_meta;
    logic sync_q;

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= a & b;
    end

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
