// Conservative CDC-004 on a gray code re-registered before the
// crossing (issue #174; header corrected in rtl-buddy-cdc#378).
//
// `gray2_q` is a plain one-cycle delay of the gray counter `gray_q`
// in the same clock. A register only changes on its clock edge, and
// consecutive values of a delayed gray sequence still differ in one
// bit, so the re-register adds latency and nothing else: the
// crossing is as safe as sampling `gray_q` directly.
//
// CDC-004 fires anyway. The structural gray detector walks back
// from the crossing flop's `D`, stops at the first flop boundary
// (`gray_q`'s Q), and never sees the `b ^ (b >> 1)` XOR shape. That
// is a known false positive, kept on purpose: the detector does not
// prove the intermediate register is an unconditional delay, and a
// load-enabled copy, which can skip codes, stops the walk the same
// way. `(* cdc_gray *)` on `gray2_q` is the escape hatch.
//
// This fixture pins that conservative verdict: a change that walks
// across the intermediate flop has to prove it is a pure delay, and
// then moves this fixture to the `good_` side.

module bad_gray_phase_break (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        incr,
    output logic [3:0]  dst_gray
);

    logic [3:0] bin_q;
    logic [3:0] gray_q;
    logic [3:0] bin_next;

    assign bin_next = bin_q + 4'd1;

    // Stage 0: canonical gray-encoded counter. `gray_q` carries the
    // correct gray code on every src_clk cycle.
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            bin_q  <= 4'd0;
            gray_q <= 4'd0;
        end else if (incr) begin
            bin_q  <= bin_next;
            gray_q <= bin_next ^ (bin_next >> 1);
        end
    end

    // Stage 1: re-register in src_clk. A pure one-cycle delay: each
    // value `gray2_q` holds is a code `gray_q` held one cycle earlier,
    // so successive values still differ in one bit. Only the
    // structural detector loses the gray shape here (see header).
    logic [3:0] gray2_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) gray2_q <= 4'd0;
        else        gray2_q <= gray_q;
    end

    // dst_clk side: textbook 4-bit 2FF synchronizer. Each lane is
    // individually filtered for metastability; the source changes
    // one bit per src_clk cycle, so the sampled value is the old or
    // the new code, never a mix of the two.
    logic [3:0] sync_1;
    logic [3:0] sync_2;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            sync_1 <= 4'd0;
            sync_2 <= 4'd0;
        end else begin
            sync_1 <= gray2_q;
            sync_2 <= sync_1;
        end
    end

    assign dst_gray = sync_2;

endmodule
