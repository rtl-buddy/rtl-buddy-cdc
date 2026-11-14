// Gray encoder written with the shifted operand first (rtl-buddy-cdc#365).
//
// The same 4-bit gray counter and ungated 4-bit 2FF synchronizer as
// `good_gray_bus_sync_chain`, except the encoder reads
// `g = (b >> 1) ^ b`. XOR is commutative, so after flattening the
// shifted operand simply sits on the `$xor`'s A input instead of B.
// The structural gray detector (`_is_gray_encoded_source`) must match
// it in either order, and CDC-004 must not fire.
//
// Paired with `good_gray_bus_sync_chain` (canonical order) and with
// the negative `bad_bus_crossing`.

module good_gray_xor_swapped (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        incr,
    output logic [3:0]  dst_gray
);

    logic [3:0] bin;
    logic [3:0] gray;
    logic [3:0] bin_next;

    assign bin_next = bin + 4'd1;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            bin  <= 4'd0;
            gray <= 4'd0;
        end else if (incr) begin
            bin  <= bin_next;
            // Shifted operand first: g = (b >> 1) ^ b.
            gray <= (bin_next >> 1) ^ bin_next;
        end
    end

    logic [3:0] sync_meta;
    logic [3:0] sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            sync_meta <= 4'd0;
            sync_q    <= 4'd0;
        end else begin
            sync_meta <= gray;
            sync_q    <= sync_meta;
        end
    end

    assign dst_gray = sync_q;

endmodule
