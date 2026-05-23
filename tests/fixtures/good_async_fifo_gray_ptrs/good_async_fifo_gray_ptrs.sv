// Positive fixture (issue #170): canonical async-FIFO pointer pair.
//
// Two pointers cross between asynchronous clock domains in opposite
// directions:
//
//   wptr_gray_q : src_clk → dst_clk  (full/empty comparison on dst)
//   rptr_gray_q : dst_clk → src_clk  (full/empty comparison on src)
//
// Both are Gray-coded — `g = b ^ (b >> 1)` — which guarantees at
// most one bit changes per source cycle. Each foreign pointer goes
// through a multi-bit 2FF synchroniser at the destination, so per-
// bit metastability is filtered. The Gray invariant guarantees any
// dst-side sample is a coherent value (the previous Gray code or
// the next, never a mid-flight mix). CDC-004 must accept the
// crossing via the structural Gray detector — no `(* cdc_gray *)`
// annotation needed.
//
// This is the single most common multi-bit CDC idiom in production
// silicon. It exercises CDC-004 (structural Gray + multi-bit sync),
// CDC-005 (reconvergent fanout — the synced pointer feeds the
// comparator only, so the filter must let it pass), and CDC-001 on
// the implicit handshake structure all at once.

module good_async_fifo_gray_ptrs #(
    parameter int PW = 4  // pointer width (depth = 1 << (PW-1) = 8)
) (
    input  logic         src_clk,
    input  logic         dst_clk,
    input  logic         rst_n,

    // Push side (src_clk)
    input  logic         push,
    output logic         full,

    // Pop side (dst_clk)
    input  logic         pop,
    output logic         empty
);

    // ---- Source-side pointer (push) ---------------------------------

    logic [PW-1:0] wptr_bin_q;
    logic [PW-1:0] wptr_gray_q;
    logic [PW-1:0] wptr_bin_next;

    assign wptr_bin_next = wptr_bin_q + {{(PW-1){1'b0}}, 1'b1};

    // Canonical Gray encoding: g = b ^ (b >> 1). Yosys flatten keeps
    // this as an $xor cell whose A/B inputs satisfy the structural
    // detector's signature.
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            wptr_bin_q  <= '0;
            wptr_gray_q <= '0;
        end else if (push && !full) begin
            wptr_bin_q  <= wptr_bin_next;
            wptr_gray_q <= wptr_bin_next ^ (wptr_bin_next >> 1);
        end
    end

    // ---- Destination-side pointer (pop) -----------------------------

    logic [PW-1:0] rptr_bin_q;
    logic [PW-1:0] rptr_gray_q;
    logic [PW-1:0] rptr_bin_next;

    assign rptr_bin_next = rptr_bin_q + {{(PW-1){1'b0}}, 1'b1};

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            rptr_bin_q  <= '0;
            rptr_gray_q <= '0;
        end else if (pop && !empty) begin
            rptr_bin_q  <= rptr_bin_next;
            rptr_gray_q <= rptr_bin_next ^ (rptr_bin_next >> 1);
        end
    end

    // ---- 2FF sync of foreign pointer into each side -----------------

    // wptr_gray crosses src_clk → dst_clk
    logic [PW-1:0] wptr_gray_meta_q;
    logic [PW-1:0] wptr_gray_sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            wptr_gray_meta_q <= '0;
            wptr_gray_sync_q <= '0;
        end else begin
            wptr_gray_meta_q <= wptr_gray_q;
            wptr_gray_sync_q <= wptr_gray_meta_q;
        end
    end

    // rptr_gray crosses dst_clk → src_clk
    logic [PW-1:0] rptr_gray_meta_q;
    logic [PW-1:0] rptr_gray_sync_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            rptr_gray_meta_q <= '0;
            rptr_gray_sync_q <= '0;
        end else begin
            rptr_gray_meta_q <= rptr_gray_q;
            rptr_gray_sync_q <= rptr_gray_meta_q;
        end
    end

    // ---- Empty / full flags -----------------------------------------
    //
    // Empty (dst_clk): local rptr_gray matches synced wptr_gray.
    // Full (src_clk):  classic gray-FIFO full-check — wrap on the top
    //   bit (the address spans 0 .. depth-1, the pointer wraps at
    //   2*depth so the gray wrap-around is detectable). Comparing
    //   {~wptr_gray[PW-1:PW-2], wptr_gray[PW-3:0]} against
    //   rptr_gray_sync_q is the textbook construction.

    assign empty = (wptr_gray_sync_q == rptr_gray_q);
    assign full  = (wptr_gray_q == {~rptr_gray_sync_q[PW-1:PW-2],
                                     rptr_gray_sync_q[PW-3:0]});

endmodule
