// Negative fixture for CDC-026 (issue #326; was the #176 coverage-gap
// sentinel `unsupported_dualport_ram_crossing`).
//
// A small RAM is written from `wr_clk` and read back, registered, on
// `rd_clk`; the SDC declares the two clocks asynchronous. Yosys keeps
// the storage as memory cells (`$memwr_v2` + an asynchronous `$memrd`
// feeding the `rd_clk` register), so no flop-to-flop crossing exists.
//
// Nothing written on `wr_clk` ever reaches the read side: the read
// address is a raw `rd_clk` input with no pointer, flag or handshake
// synchronised from the write domain. The reader cannot know which
// words are stable, so a read can sample a word mid-write. CDC-026
// must fire, naming the memory and both clocks — and nothing else.

module bad_dualport_ram_crossing #(
    parameter int AW = 4,
    parameter int DW = 8
) (
    input  logic            wr_clk,
    input  logic            wr_rst_n,
    input  logic            wr_en,
    input  logic [AW-1:0]   wr_addr,
    input  logic [DW-1:0]   wr_data,

    input  logic            rd_clk,
    input  logic            rd_rst_n,
    input  logic [AW-1:0]   rd_addr,
    output logic [DW-1:0]   rd_data
);

    // Behavioural dual-clock storage. Yosys keeps this as memory cells
    // under the `lint` wrapper's `proc; flatten; opt_clean` (the flow
    // this fixture's netlist is built with — no `memory` pass).
    logic [DW-1:0] mem [(1<<AW)-1:0];

    always_ff @(posedge wr_clk) begin
        if (wr_en) mem[wr_addr] <= wr_data;
    end

    // Registered read on rd_clk: mem[rd_addr] is the storage written by
    // wr_clk, and rd_addr carries no write-domain information.
    always_ff @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n) rd_data <= '0;
        else           rd_data <= mem[rd_addr];
    end

endmodule
