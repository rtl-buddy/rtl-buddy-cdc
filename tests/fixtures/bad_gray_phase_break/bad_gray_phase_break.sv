// Anti-pattern fixture (issue #174): a Gray-coded value is re-
// registered in the source clock domain *before* being crossed.
//
// Gray's load-bearing invariant is that at most one bit flips per
// source cycle. The intermediate re-register breaks it: at any
// destination sample point, `gray2_q` can hold the previous Gray
// value while `gray_q` already holds the next — a downstream sync
// chain reading `gray2_q` sees a multi-bit transition mid-flight,
// exactly what the encoding was supposed to prevent.
//
// The crossing flop is `gray2_q`, not `gray_q`. The gray-encoding
// structural detector in rules.py walks back from `gray2_q.D`,
// hits the producer's Q-pin (a flop boundary), and stops without
// finding the canonical `b ^ (b >> 1)` XOR shape. Therefore the
// crossing is not exempted via the structural gray path, and
// CDC-004 must fire.
//
// This fixture pins that detector behaviour as a regression
// sentinel: future tightening (or any change that walks across an
// intermediate flop) would surface here.

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

    // Stage 1: redundant re-register in src_clk. THIS IS THE BUG.
    // `gray2_q` lags `gray_q` by one cycle — during the cycle when
    // `gray_q` advances, `gray2_q` still holds the previous value;
    // multi-bit transitions become visible at the crossing point.
    logic [3:0] gray2_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) gray2_q <= 4'd0;
        else        gray2_q <= gray_q;
    end

    // dst_clk side: textbook 4-bit 2FF synchronizer. Each lane is
    // individually filtered for metastability, but the source's
    // multi-bit invariant is broken (see above), so the sampled
    // value can be a transient mix.
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
