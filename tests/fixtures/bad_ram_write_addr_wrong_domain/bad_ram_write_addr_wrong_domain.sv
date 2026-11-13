// Negative fixture (issue #326): a memory write port fed from the
// wrong clock domain.
//
// The RAM is written and read on `wr_clk` only — the storage itself
// never crosses a clock, so CDC-026 has nothing to say. But the write
// *address* is a counter clocked on the asynchronous `rd_clk`, wired
// straight into the `wr_clk` write port with no synchroniser. A memory
// write port is a sink in its write-clock domain, so this is an
// ordinary unsynchronised multi-bit crossing into it: CDC-004 must fire
// on the counter -> write-port path, and nothing else.

module bad_ram_write_addr_wrong_domain (
    input  logic       wr_clk,
    input  logic       rd_clk,
    input  logic       rst_n,
    input  logic       wr_en,
    input  logic [7:0] wr_data,
    input  logic [3:0] rd_addr,
    output logic [7:0] rd_q
);

    logic [7:0] mem [0:15];

    logic [3:0] slot_q;
    always_ff @(posedge rd_clk or negedge rst_n) begin
        if (!rst_n) slot_q <= '0;
        else        slot_q <= slot_q + 4'd1;
    end

    always_ff @(posedge wr_clk) begin
        if (wr_en) mem[slot_q] <= wr_data;
        rd_q <= mem[rd_addr];
    end

endmodule
