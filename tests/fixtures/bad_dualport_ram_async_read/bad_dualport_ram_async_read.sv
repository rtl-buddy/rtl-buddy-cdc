// Negative fixture for CDC-026 (issue #326): asynchronous-read variant.
//
// The RAM is written on `wr_clk`. The read side reads it combinationally
// (`mem[rd_addr]`, an asynchronous read port) and folds the word through
// a little logic (`^ rd_mask`) before an `rd_clk` register captures it —
// so the write domain passes through the read port and the XOR straight
// into `rd_clk`. The read address is a free-running `rd_clk` counter
// with no pointer or flag synchronised from `wr_clk`, so CDC-026 must
// fire, with the read domain taken from the capturing register.

module bad_dualport_ram_async_read (
    input  logic       wr_clk,
    input  logic       rd_clk,
    input  logic       rd_rst_n,
    input  logic       wr_en,
    input  logic [3:0] wr_addr,
    input  logic [7:0] wr_data,
    input  logic [7:0] rd_mask,
    output logic [7:0] rd_q
);

    logic [7:0] mem [0:15];

    always_ff @(posedge wr_clk) begin
        if (wr_en) mem[wr_addr] <= wr_data;
    end

    logic [3:0] rd_addr_q;
    always_ff @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n) rd_addr_q <= '0;
        else           rd_addr_q <= rd_addr_q + 4'd1;
    end

    logic [7:0] rd_word;
    assign rd_word = mem[rd_addr_q] ^ rd_mask;

    always_ff @(posedge rd_clk) rd_q <= rd_word;

endmodule
