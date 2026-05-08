// Positive counterpart to bad_port_no_sync: same scenario but with a
// proper 2FF synchronizer in dst_clk. The port is typed via
// set_input_delay -clock foreign_clk, the analyzer promotes the
// port→flop path to a first-class crossing, but CDC-001 sees the
// chain depth ≥ 2 and stays silent.

module good_port_typed_sync (
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    (* cdc_sync *) logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= d_in;
    end

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
