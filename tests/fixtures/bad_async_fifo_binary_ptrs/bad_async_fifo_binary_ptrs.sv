// Negative counterpart to `good_async_fifo_gray_ptrs` (issue #170).
//
// Same async-FIFO structure, but the wptr / rptr pointers are
// crossed in plain BINARY instead of Gray code. With multi-bit
// binary pointers crossing an async boundary, individual lanes can
// settle on different destination cycles — the dst-side sample can
// be a transient mix of the old and new pointer values that never
// existed as a real state. CDC-004 must fire on both crossings.
//
// This fixture exists to confirm that:
// 1. The pointer-pair shape (two opposite-direction multi-bit
//    crossings) doesn't accidentally pass CDC-004 via some other
//    exemption (gating, structural sync, etc.).
// 2. The good fixture's clean result is not trivial — the SAME
//    structure with Gray dropped fails as expected.

module bad_async_fifo_binary_ptrs #(
    parameter int PW = 4
) (
    input  logic         src_clk,
    input  logic         dst_clk,
    input  logic         rst_n,

    input  logic         push,
    output logic         full,

    input  logic         pop,
    output logic         empty
);

    // ---- Source-side pointer (push) — BINARY (the bug) --------------

    logic [PW-1:0] wptr_bin_q;
    logic [PW-1:0] wptr_bin_next;

    assign wptr_bin_next = wptr_bin_q + {{(PW-1){1'b0}}, 1'b1};

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)              wptr_bin_q <= '0;
        else if (push && !full)  wptr_bin_q <= wptr_bin_next;
    end

    // ---- Destination-side pointer (pop) — BINARY (the bug) ----------

    logic [PW-1:0] rptr_bin_q;
    logic [PW-1:0] rptr_bin_next;

    assign rptr_bin_next = rptr_bin_q + {{(PW-1){1'b0}}, 1'b1};

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n)             rptr_bin_q <= '0;
        else if (pop && !empty) rptr_bin_q <= rptr_bin_next;
    end

    // ---- 2FF sync of foreign pointer into each side -----------------
    // Per-bit metastability is filtered, but the multi-bit invariant
    // is gone — different lanes can land on different dst cycles.

    logic [PW-1:0] wptr_bin_meta_q;
    logic [PW-1:0] wptr_bin_sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            wptr_bin_meta_q <= '0;
            wptr_bin_sync_q <= '0;
        end else begin
            wptr_bin_meta_q <= wptr_bin_q;
            wptr_bin_sync_q <= wptr_bin_meta_q;
        end
    end

    logic [PW-1:0] rptr_bin_meta_q;
    logic [PW-1:0] rptr_bin_sync_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            rptr_bin_meta_q <= '0;
            rptr_bin_sync_q <= '0;
        end else begin
            rptr_bin_meta_q <= rptr_bin_q;
            rptr_bin_sync_q <= rptr_bin_meta_q;
        end
    end

    // Empty / full from binary comparison. Even with a 2FF sync the
    // multi-bit sample is incoherent without the Gray invariant.
    assign empty = (wptr_bin_sync_q == rptr_bin_q);
    assign full  = (wptr_bin_q == {~rptr_bin_sync_q[PW-1:PW-2],
                                    rptr_bin_sync_q[PW-3:0]});

endmodule
