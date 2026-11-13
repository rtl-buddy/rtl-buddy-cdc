// Positive fixture for CDC-026 (issue #326): the textbook async FIFO
// built on an inferred dual-clock RAM.
//
// The RAM (16 x 8) is written on `wr_clk` at the binary write pointer
// and read on `rd_clk` at the binary read pointer. Each pointer also
// keeps a gray copy (`g = b ^ (b >> 1)`) that crosses to the other side
// through a multi-bit 2FF synchroniser:
//
//   wptr_gray_q : wr_clk -> rd_clk  (empty flag, gates the read pointer)
//   rptr_gray_q : rd_clk -> wr_clk  (full flag, gates the write pointer)
//
// The read port's address is the read pointer, which only advances
// under `pop && !empty`, and `empty` compares against the synchronised
// write pointer — so the read side's address logic receives the
// write-domain crossing. That is the sanctioned shape: CDC-026 stays
// silent, and CDC-004 accepts both pointer crossings via the structural
// gray detector. Two read ports exercise both read-domain paths: the
// registered `rd_data` (captured by an `rd_clk` flop) and the
// first-word-fall-through `rd_peek` (asynchronous read straight to an
// output port, whose domain comes from its address).
//
// The flags are assigned at the bottom, after the procedural blocks
// that read them (a forward use both frontends must resolve). Built
// with the `lint` wrapper's `proc; flatten; opt_clean`.

module good_async_fifo_inferred_ram #(
    parameter int AW = 4,
    parameter int DW = 8
) (
    input  logic          wr_clk,
    input  logic          rd_clk,
    input  logic          rst_n,

    input  logic          push,
    input  logic [DW-1:0] wr_data,
    output logic          full,

    input  logic          pop,
    output logic [DW-1:0] rd_data,
    output logic [DW-1:0] rd_peek,
    output logic          empty
);

    logic [DW-1:0] mem [(1<<AW)-1:0];

    logic [AW:0] wptr_bin_q, wptr_gray_q, wptr_bin_next;
    logic [AW:0] rptr_bin_q, rptr_gray_q, rptr_bin_next;
    logic [AW:0] wptr_gray_meta_q, wptr_gray_sync_q;
    logic [AW:0] rptr_gray_meta_q, rptr_gray_sync_q;

    // ---- Write side (wr_clk) ----------------------------------------

    assign wptr_bin_next = wptr_bin_q + 1'b1;

    always_ff @(posedge wr_clk or negedge rst_n) begin
        if (!rst_n) begin
            wptr_bin_q  <= '0;
            wptr_gray_q <= '0;
        end else if (push && !full) begin
            wptr_bin_q  <= wptr_bin_next;
            wptr_gray_q <= wptr_bin_next ^ (wptr_bin_next >> 1);
        end
    end

    always_ff @(posedge wr_clk) begin
        if (push && !full) mem[wptr_bin_q[AW-1:0]] <= wr_data;
    end

    // ---- Read side (rd_clk) -----------------------------------------

    assign rptr_bin_next = rptr_bin_q + 1'b1;

    always_ff @(posedge rd_clk or negedge rst_n) begin
        if (!rst_n) begin
            rptr_bin_q  <= '0;
            rptr_gray_q <= '0;
        end else if (pop && !empty) begin
            rptr_bin_q  <= rptr_bin_next;
            rptr_gray_q <= rptr_bin_next ^ (rptr_bin_next >> 1);
        end
    end

    always_ff @(posedge rd_clk) begin
        if (pop && !empty) rd_data <= mem[rptr_bin_q[AW-1:0]];
    end

    assign rd_peek = mem[rptr_bin_q[AW-1:0]];

    // ---- Pointer synchronisers --------------------------------------

    always_ff @(posedge rd_clk or negedge rst_n) begin
        if (!rst_n) begin
            wptr_gray_meta_q <= '0;
            wptr_gray_sync_q <= '0;
        end else begin
            wptr_gray_meta_q <= wptr_gray_q;
            wptr_gray_sync_q <= wptr_gray_meta_q;
        end
    end

    always_ff @(posedge wr_clk or negedge rst_n) begin
        if (!rst_n) begin
            rptr_gray_meta_q <= '0;
            rptr_gray_sync_q <= '0;
        end else begin
            rptr_gray_meta_q <= rptr_gray_q;
            rptr_gray_sync_q <= rptr_gray_meta_q;
        end
    end

    // ---- Flags --------------------------------------------------------

    assign empty = (wptr_gray_sync_q == rptr_gray_q);
    assign full  = (wptr_gray_q == {~rptr_gray_sync_q[AW:AW-1],
                                     rptr_gray_sync_q[AW-2:0]});

endmodule
