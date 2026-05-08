// Positive counterpart to bad_single_ff_sync.
//
// Standard 2FF level synchronizer. The source-domain flop is sampled
// by the destination's first sync stage (chain depth = 2), no
// combinational logic on the path. CDC-001 / CDC-002 / CDC-003 / CDC-005
// all stay silent. The textbook minimum-correctness pattern.

module good_2ff_sync (
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

    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_q;
    end

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
