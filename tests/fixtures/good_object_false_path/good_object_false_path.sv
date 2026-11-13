// Positive fixture for an object-scoped set_false_path (#328).
//
// A configuration block u_cfg holds an 8-bit mode register and a 1-bit
// enable, both written in src_clk and read in dst_clk by single capture
// flops with no synchroniser. Structurally that is CDC-004 (the bus)
// and CDC-001 (the bit). The SDC declares both sources static with
// object-scoped false paths written the way synthesis SDCs name
// registers (u_cfg/mode_q_reg*, u_cfg/enable_q_reg/Q), so both findings
// are acknowledged: reported as suppressed with the SDC statement as
// the reason, not dropped, and the run exits clean.

module cfg_regs (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       we,
    input  logic [7:0] wdata,
    output logic [7:0] mode,
    output logic       enable
);
    logic [7:0] mode_q;
    logic       enable_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            mode_q   <= 8'h00;
            enable_q <= 1'b0;
        end else if (we) begin
            mode_q   <= wdata;
            enable_q <= wdata[0];
        end
    end
    assign mode   = mode_q;
    assign enable = enable_q;
endmodule

module good_object_false_path (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       rst_n,
    input  logic       we,
    input  logic [7:0] wdata,
    output logic [7:0] mode_out,
    output logic       en_out
);
    logic [7:0] mode;
    logic       enable;
    cfg_regs u_cfg (
        .clk(src_clk), .rst_n(rst_n), .we(we), .wdata(wdata),
        .mode(mode), .enable(enable)
    );

    logic [7:0] mode_dst;
    logic       en_dst;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            mode_dst <= 8'h00;
            en_dst   <= 1'b0;
        end else begin
            mode_dst <= mode;
            en_dst   <= enable;
        end
    end
    assign mode_out = mode_dst;
    assign en_out   = en_dst;
endmodule
