// Negative-case fixture: a top-level input port typed via
// set_input_delay -clock foreign_clk drives a flop in dst_clk
// directly, with no synchronizer chain. CDC-001 must fire on this
// port→flop crossing — the destination flop has chain depth 1 (just
// itself), and the SDC declares the two clocks async.

module bad_port_no_sync (
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic dst_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= d_in;
    end

    assign q_out = dst_q;

endmodule
