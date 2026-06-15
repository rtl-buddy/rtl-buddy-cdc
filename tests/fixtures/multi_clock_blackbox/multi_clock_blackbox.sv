// Multi-clock blackbox soundness fixture (rtl-buddy-cdc#259 audit, FIX 1).
//
// `afifo` is a dual-clock IP (an async-FIFO shape) whose clock pins are
// `wr_clk` and `rd_clk` — names that are NOT in the summariser's
// clock-pin allow-list. It carries a REAL internal wr_clk -> rd_clk
// crossing: a wr_clk register drives a rd_clk register directly.
//
// The bug FIX 1 closes: the pre-#259 `_instance_clock` only recognised a
// clock pin by NAME, so when `afifo` is blackboxed its clock set
// resolved to a single domain (or none) and the block was abstracted as
// if it were single-clock / combinational — its internal clkA->clkB
// crossing VANISHED silently.
//
// FLAT: the wr_clk -> rd_clk crossing fires (CDC-004 / CDC-001).
// BLACKBOXED: the dual-clock block is DECLINED (>=2 distinct clock
// roots), a partial_warning names it, and the run does NOT falsely
// report it clean.

module afifo (
    input  wire       wr_clk,
    input  wire       rd_clk,
    input  wire [7:0] wdata,
    output wire [7:0] rdata
);
    reg [7:0] wr_stage;
    reg [7:0] rd_stage;
    // Write-domain register.
    always_ff @(posedge wr_clk) wr_stage <= wdata;
    // Read-domain register captures the write-domain data DIRECTLY — the
    // real wr_clk -> rd_clk async crossing inside the IP.
    always_ff @(posedge rd_clk) rd_stage <= wr_stage;
    assign rdata = rd_stage;
endmodule

module top (
    input  wire       wr_clk,
    input  wire       rd_clk,
    input  wire [7:0] din,
    output wire [7:0] dout
);
    reg [7:0] src_q;
    reg [7:0] dst_q;

    always_ff @(posedge wr_clk) src_q <= din;

    wire [7:0] fifo_rd;
    afifo u_afifo (
        .wr_clk(wr_clk),
        .rd_clk(rd_clk),
        .wdata (src_q),
        .rdata (fifo_rd)
    );

    always_ff @(posedge rd_clk) dst_q <= fifo_rd;
    assign dout = dst_q;
endmodule
