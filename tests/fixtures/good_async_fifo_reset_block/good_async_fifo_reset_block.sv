// Async FIFO whose storage write sits in the write pointer's
// asynchronous-reset block (rtl-buddy-cdc#414). Because of the reset,
// Yosys mem2reg (and the slang frontend, rtl-buddy-cdc#401) build `mem`
// as DEPTH registers loaded through a decode of the write address and
// read through a mux on the read address, not as a memory cell; the read
// side then captures each word in two registers (op / arg), which used
// to fire CDC-020 once per word. The array has exactly the memory shape
// -- one write decode on wclk, one read mux on rbin -- so the analyzer
// judges it as the memory it is: the gray write pointer synchronised
// into rclk gates every read (CDC-026 is satisfied), and nothing fires.
module sync2 #(
    parameter int W = 1
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic [W-1:0] d,
    output logic [W-1:0] q
);
    logic [W-1:0] meta;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) begin
            meta <= '0;
            q    <= '0;
        end else begin
            meta <= d;
            q    <= meta;
        end
endmodule

module good_async_fifo_reset_block #(
    parameter int DEPTH  = 8,
    parameter int DATA_W = 12
) (
    input  logic              wclk,
    input  logic              wrst_n,
    input  logic              wr_en,
    input  logic [DATA_W-1:0] wr_data,
    output logic              wr_full,
    input  logic              rclk,
    input  logic              rrst_n,
    input  logic              rd_en,
    output logic [3:0]        op,
    output logic [7:0]        arg,
    output logic              rd_empty
);
    localparam int AW = $clog2(DEPTH);

    logic [DATA_W-1:0] mem [DEPTH];
    logic [AW:0] wbin, wgray, rbin, rgray, wgray_r, rgray_w;
    logic [AW:0] wbin_n, rbin_n, wgray_n, rgray_n;
    assign wbin_n  = wbin + 1'b1;
    assign rbin_n  = rbin + 1'b1;
    assign wgray_n = wbin_n ^ (wbin_n >> 1);
    assign rgray_n = rbin_n ^ (rbin_n >> 1);

    always_ff @(posedge wclk or negedge wrst_n)
        if (!wrst_n) begin
            wbin  <= '0;
            wgray <= '0;
        end else if (wr_en && !wr_full) begin
            mem[wbin[AW-1:0]] <= wr_data;
            wbin  <= wbin_n;
            wgray <= wgray_n;
        end

    sync2 #(.W(AW + 1)) u_sync_r2w (.clk(wclk), .rst_n(wrst_n), .d(rgray), .q(rgray_w));
    assign wr_full = wgray_n == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]};

    always_ff @(posedge rclk or negedge rrst_n)
        if (!rrst_n) begin
            rbin  <= '0;
            rgray <= '0;
        end else if (rd_en && !rd_empty) begin
            rbin  <= rbin_n;
            rgray <= rgray_n;
        end

    sync2 #(.W(AW + 1)) u_sync_w2r (.clk(rclk), .rst_n(rrst_n), .d(wgray), .q(wgray_r));
    assign rd_empty = rgray == wgray_r;

    logic [DATA_W-1:0] rd_data;
    assign rd_data = mem[rbin[AW-1:0]];
    always_ff @(posedge rclk or negedge rrst_n)
        if (!rrst_n) begin
            op  <= '0;
            arg <= '0;
        end else if (rd_en && !rd_empty) begin
            op  <= rd_data[11:8];
            arg <= rd_data[7:0];
        end
endmodule
