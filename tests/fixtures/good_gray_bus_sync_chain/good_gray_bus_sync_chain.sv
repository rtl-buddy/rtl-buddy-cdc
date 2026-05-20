// Positive coverage for CDC-004's structural-gray-coded acceptance.
//
// A 4-bit gray-coded counter increments freely in src_clk and is
// sampled by an *ungated* 4-bit 2FF synchronizer in dst_clk. The
// destination samples every cycle — there is no handshake or load
// enable — so the only thing keeping this bus correct is the gray
// encoding. CDC-004's structural detector pairs the canonical
// `g = b ^ (b >> 1)` pattern at the source with the multi-bit sync
// chain at the destination (rules.py `_is_multibit_sync_first_stage`
// + `_is_gray_encoded_source`) and must not fire.
//
// Paired with the gated counterpart `good_gray_counter_crossing` and
// with the negative `bad_bus_crossing`.

module good_gray_bus_sync_chain (
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

    // Multi-bit 2FF synchronizer in dst_clk — ungated. Correctness
    // relies entirely on the gray encoding at the source.
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
