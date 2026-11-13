// Negative fixture: an object-scoped false path does not hide
// reconvergence (#328).
//
// A static configuration bit in u_cfg fans out to two independent 2FF
// synchronizer chains in dst_clk whose outputs recombine. The SDC
// false-paths the source register, which acknowledges the
// missing-synchroniser class of findings, but CDC-005 is a hazard of
// the destination's recombination, not of the source toggling, so it
// must still fire (the same choice (* cdc_static *) makes).

module cfg_bit (
    input  logic clk,
    input  logic rst_n,
    input  logic d,
    output logic q
);
    logic flag_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) flag_q <= 1'b0;
        else        flag_q <= d;
    end
    assign q = flag_q;
endmodule

module bad_object_false_path_reconvergent (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);
    logic flag;
    cfg_bit u_cfg (.clk(src_clk), .rst_n(rst_n), .d(d_in), .q(flag));

    logic sync_a_meta, sync_a_q;
    logic sync_b_meta, sync_b_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_meta <= 1'b0;
        else        sync_a_meta <= flag;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_q <= 1'b0;
        else        sync_a_q <= sync_a_meta;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_meta <= 1'b0;
        else        sync_b_meta <= flag;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_q <= 1'b0;
        else        sync_b_q <= sync_b_meta;
    end
    assign q_out = sync_a_q & ~sync_b_q;
endmodule
