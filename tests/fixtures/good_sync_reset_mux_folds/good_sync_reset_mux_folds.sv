// Synchronisers whose sync resets `opt_dff` folds and a one-level,
// whole-vector look-through did not (rtl-buddy-cdc#390).
//
//   a1 / a2  two sync resets of one value, chained muxes on D
//   g2       same, on the second stage of a gray-bus synchroniser
//   bs       packed 2-stage shift, reset on the first lane only
//
// `opt_dff` folds per lane, through every constant-leg mux whose
// constant matches the lane's reset value: a1 / a2 / g2 become `$sdff`s
// with SRST = !rst_n | flush, and bs is sliced into an `$sdff` (lane 0)
// and a `$dff` (lane 1). The `proc` form keeps the muxes; reading them
// one level and whole-vector only, it saw a depth-1 chain behind a1
// (CDC-001), no second stage behind g1 (CDC-004) and a gate on the way
// into bs (CDC-003). All are clean synchronisers in every form. The
// committed `.optdff.json` holds the `opt_dff` form and must agree.

module good_sync_reset_mux_folds (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic flush,
    input  logic din_a,
    input  logic din_b,
    input  logic incr,
    output logic q_a,
    output logic q_b,
    output logic [1:0] q_g
);
    logic a_q, b_q;
    always_ff @(posedge src_clk) begin
        a_q <= din_a;
        b_q <= din_b;
    end

    logic a1, a2;
    always_ff @(posedge dst_clk)
        if (!rst_n)     a1 <= 1'b0;
        else if (flush) a1 <= 1'b0;
        else            a1 <= a_q;
    always_ff @(posedge dst_clk)
        if (!rst_n)     a2 <= 1'b0;
        else if (flush) a2 <= 1'b0;
        else            a2 <= a1;
    assign q_a = a2;

    logic [1:0] bin, gray, g1, g2;
    wire  [1:0] bin_next = bin + 2'd1;
    always_ff @(posedge src_clk)
        if (incr) begin
            bin  <= bin_next;
            gray <= bin_next ^ (bin_next >> 1);
        end
    always_ff @(posedge dst_clk) g1 <= gray;
    always_ff @(posedge dst_clk)
        if (!rst_n)     g2 <= 2'b00;
        else if (flush) g2 <= 2'b00;
        else            g2 <= g1;
    assign q_g = g2;

    logic [1:0] bs;
    always_ff @(posedge dst_clk) begin
        if (!rst_n) bs[0] <= 1'b0;
        else        bs[0] <= b_q;
        bs[1] <= bs[0];
    end
    assign q_b = bs[1];
endmodule
