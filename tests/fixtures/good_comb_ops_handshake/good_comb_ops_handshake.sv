// Wider combinational operators on a handshake-held crossing
// (rtl-buddy-cdc#331).
//
// A req/ack handshake holds the src_clk operands a_q, b_q and idx_q
// stable, and dst_clk captures a combinational function of them on the
// synchronised request: `$mul` (a_q * b_q), `$le`, `$reduce_xor`,
// `$reduce_or` and `$shiftx` (a_q[idx_q]). The logic sits on the
// crossing, but its inputs do not change while the capture enable is
// high: the capture is a gated bus (CDC-004 silent) with an acknowledge
// path (CDC-012 silent). Nothing fires.
//
// Known gap, pinned as a strict xfail: the slang frontend does not lower
// a_q[idx_q], so the whole concatenation becomes an opaque driver and
// slang sees two crossings instead of five (the verdict is clean either
// way).

module good_comb_ops_handshake (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        src_req,
    input  logic [3:0]  src_a,
    input  logic [3:0]  src_b,
    input  logic [1:0]  src_idx,
    output logic [11:0] dst_result
);
    // --- src_clk: held request and operands, acknowledge synchroniser ---
    logic dst_ack;
    logic ack_meta, ack_sync;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) begin
            ack_meta <= 1'b0;
            ack_sync <= 1'b0;
        end else begin
            ack_meta <= dst_ack;
            ack_sync <= ack_meta;
        end

    logic src_load_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n)         src_load_q <= 1'b0;
        else if (src_req)   src_load_q <= 1'b1;
        else if (ack_sync)  src_load_q <= 1'b0;

    logic [3:0] a_q, b_q;
    logic [1:0] idx_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) begin
            a_q   <= '0;
            b_q   <= '0;
            idx_q <= '0;
        end else if (src_req && !src_load_q && !ack_sync) begin
            a_q   <= src_a;
            b_q   <= src_b;
            idx_q <= src_idx;
        end

    // Combinational function of the held operands.
    logic [11:0] result;
    assign result = {a_q * b_q, a_q <= b_q, ^a_q, |b_q, a_q[idx_q]};

    // --- dst_clk: request synchroniser, capture, acknowledge ---
    logic load_s1, load_s2;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) begin
            load_s1    <= 1'b0;
            load_s2    <= 1'b0;
            dst_ack    <= 1'b0;
            dst_result <= '0;
        end else begin
            load_s1 <= src_load_q;
            load_s2 <= load_s1;
            dst_ack <= load_s2;
            if (load_s2) dst_result <= result;
        end
endmodule
