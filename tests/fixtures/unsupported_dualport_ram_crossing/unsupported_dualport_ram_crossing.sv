// Sentinel fixture for a known coverage gap (issue #176): rtl-buddy-cdc
// is a flop-based analyzer and does not model dual-port-RAM clock-
// domain crossings.
//
// This design writes a small register array from `wr_clk` and reads
// it back from `rd_clk`. The two clocks are declared asynchronous in
// the SDC. In real silicon the read-port samples can be metastable
// (the storage is not synchronised) — a vendor memory-compiler CDC
// report is the right tool for this shape. rtl-buddy-cdc currently
// reports nothing on the memory-side data path; the paired test pins
// that behaviour so future changes either keep it or surface
// explicitly as a regression.

module unsupported_dualport_ram_crossing #(
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

    // Behavioural dual-clock storage. Yosys keeps this as a $mem
    // cell under the default `proc; flatten` flow used by the
    // fixture build script — no synthesis pass expands it to flops.
    logic [DW-1:0] mem [(1<<AW)-1:0];

    always_ff @(posedge wr_clk) begin
        if (wr_en) mem[wr_addr] <= wr_data;
    end

    // Registered read on rd_clk. The read flop is in rd_clk's domain;
    // its D pin is fed by mem[rd_addr], which is the storage written
    // by wr_clk. The analyzer cannot reason across the $mem boundary,
    // so this cross-domain hazard is invisible to the rule pack.
    always_ff @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n) rd_data <= '0;
        else           rd_data <= mem[rd_addr];
    end

endmodule
