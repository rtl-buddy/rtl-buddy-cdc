// Positive counterpart to bad_rdc_003_sync_reset_crossing.
//
// Same source flop (`src_rst` in src_clk), but the destination's
// SRST now comes from a 2FF reset synchroniser in dst_clk
// (`src_rst_meta` → `src_rst_sync`). The async-to-sync sample step
// happens inside the synchroniser, not at the consumer's SRST pin,
// so the consuming flop is safe.
//
// RDC-003 stays silent because the immediate driver of the
// consumer's SRST is `src_rst_sync` — a flop in the same clock
// domain (dst_clk) as the consumer.
//
// Build (same as the bad fixture; `opt_dff` is required to fold the
// mux-on-D shape into `$sdff`):
//   yosys -p 'read_verilog -sv good_rdc_003_sync_reset_synced.sv;
//             hierarchy -top good_rdc_003_sync_reset_synced;
//             proc; opt_dff; flatten; opt_clean;
//             write_json good_rdc_003_sync_reset_synced.json'

module good_rdc_003_sync_reset_synced (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic kill_req,
    input  logic d_in,
    output logic q_out
);

    logic src_rst;
    always_ff @(posedge src_clk or negedge global_rst_n) begin
        if (!global_rst_n) src_rst <= 1'b0;
        else               src_rst <= kill_req;
    end

    // 2FF reset synchroniser in dst_clk.
    logic src_rst_meta, src_rst_sync;
    always_ff @(posedge dst_clk or negedge global_rst_n) begin
        if (!global_rst_n) begin
            src_rst_meta <= 1'b0;
            src_rst_sync <= 1'b0;
        end else begin
            src_rst_meta <= src_rst;
            src_rst_sync <= src_rst_meta;
        end
    end

    logic q_dst;
    always_ff @(posedge dst_clk) begin
        if (src_rst_sync) q_dst <= 1'b0;
        else              q_dst <= d_in;
    end

    assign q_out = q_dst;

endmodule
