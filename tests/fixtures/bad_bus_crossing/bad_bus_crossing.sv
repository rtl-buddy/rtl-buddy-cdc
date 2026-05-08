// Negative-case fixture: an 8-bit data bus crosses clock domains with
// no handshake, no gray-coding, and no gating. Each bit is independently
// metastable on the dst side, so even though every bit goes through a
// 2FF synchronizer the bus value as a whole can be sampled mid-flight
// (some bits old, some bits new). Should trip CDC-004.

module bad_bus_crossing (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic [7:0]  src_data,
    output logic [7:0]  dst_data
);

    logic [7:0] src_q;
    logic [7:0] sync_stage0;
    logic [7:0] sync_stage1;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= '0;
        else        src_q <= src_data;
    end

    // BAD: the bus is synchronized bit-by-bit but never gated by a
    // handshake or recoded as gray. Adjacent bits crossing
    // simultaneously can land on different dst cycles.
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            sync_stage0 <= '0;
            sync_stage1 <= '0;
        end else begin
            sync_stage0 <= src_q;
            sync_stage1 <= sync_stage0;
        end
    end

    assign dst_data = sync_stage1;

endmodule
