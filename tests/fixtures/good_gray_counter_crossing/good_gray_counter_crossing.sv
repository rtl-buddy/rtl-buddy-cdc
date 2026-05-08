// Positive counterpart to bad_bus_crossing for the gray-code path.
//
// A 4-bit gray-coded counter increments in src_clk and is sampled by a
// 4-bit 2FF synchronizer in dst_clk. Each individual bit is filtered
// for metastability by the synchronizer; the gray encoding guarantees
// at most one bit flips per source cycle, so the sampled value is
// always either the previous gray value or the next — never a
// transient mix. CDC-004 must not fire.

module good_gray_counter_crossing (
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
            // Canonical gray encoding: g = b ^ (b >> 1).
            gray <= bin_next ^ (bin_next >> 1);
        end
    end

    // Multi-bit 2FF synchronizer in dst_clk.
    logic [3:0] sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 4'd0;
        else        sync_meta <= gray;
    end

    logic [3:0] sync_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 4'd0;
        else        sync_q <= sync_meta;
    end

    assign dst_gray = sync_q;

endmodule
