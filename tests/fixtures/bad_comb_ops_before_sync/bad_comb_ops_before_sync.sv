// Wider combinational operators before synchronisers (rtl-buddy-cdc#331).
//
// Five status bits are computed from the src_clk registers a_q, b_q and
// idx_q in front of five 2FF synchronisers, one operator family each:
// `$mul` + `$reduce_or` (|(a_q * b_q)), `$lt` (a_q < b_q), `$reduce_xor`
// (^a_q), `$reduce_and` (&b_q) and `$shiftx` (a_q[idx_q]). Every source
// register reaching a synchroniser through that logic is a CDC-003: eight
// crossings, eight findings. a_q and b_q each reach several independent
// synchronisers, so CDC-020 fires once for each.
//
// Known gap, pinned as a strict xfail: the slang frontend does not lower
// the run-time index read a_q[idx_q]. It warns and models it as an
// opaque driver, so slang finds six crossings instead of eight.

module bad_comb_ops_before_sync (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic [3:0] a,
    input  logic [3:0] b,
    input  logic [1:0] idx,
    output logic       prod_any,
    output logic       a_lt_b,
    output logic       a_parity,
    output logic       b_all,
    output logic       a_sel
);
    logic [3:0] a_q, b_q;
    logic [1:0] idx_q;
    always_ff @(posedge src_clk) begin
        a_q   <= a;
        b_q   <= b;
        idx_q <= idx;
    end

    // BAD: each synchroniser samples a combinational function of src_clk
    // registers.
    logic [7:0] prod;
    assign prod = a_q * b_q;

    logic prod_s1, lt_s1, par_s1, all_s1, sel_s1;
    always_ff @(posedge dst_clk) begin
        prod_s1  <= |prod;
        lt_s1    <= a_q < b_q;
        par_s1   <= ^a_q;
        all_s1   <= &b_q;
        sel_s1   <= a_q[idx_q];
        prod_any <= prod_s1;
        a_lt_b   <= lt_s1;
        a_parity <= par_s1;
        b_all    <= all_s1;
        a_sel    <= sel_s1;
    end
endmodule
