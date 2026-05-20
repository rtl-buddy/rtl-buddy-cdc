// Mark a conventional 2FF synchronizer with `(* cdc_sync *)` to
// declare the first stage as user-vetted. The structural depth keeps
// both rtl-buddy-cdc and SpyGlass clean; the attribute exercises the
// marker plumbing without relying on a single-stage waiver.

module marked_user_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // (* cdc_sync *) marks the first stage of a conventional 2FF
    // synchronizer.
    (* cdc_sync = "level_2ff" *) logic dst_meta;
    logic dst_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            dst_meta <= 1'b0;
            dst_q    <= 1'b0;
        end else begin
            dst_meta <= src_q;
            dst_q    <= dst_meta;
        end
    end

    assign q_out = dst_q;

endmodule
