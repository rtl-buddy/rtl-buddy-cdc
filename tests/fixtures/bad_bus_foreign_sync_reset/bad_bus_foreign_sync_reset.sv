// Buses into dst_clk registers whose synchronous reset comes from
// another clock domain, in the `proc` and the `opt_dff` form
// (rtl-buddy-cdc#383).
//
// `r` is a src_clk register used as the synchronous reset of two 4-bit
// dst_clk registers:
//
//   cap   loads `pay` under `en_s2`, a 2FF-synchronised request, with no
//         acknowledge back to src_clk: a gated capture (CDC-012 on pay
//         and on r, which reaches all four lanes through the reset)
//   gq    second stage of a 2FF gray-code bus synchroniser (`gray` ->
//         `meta` -> `gq`): accepted; r -> gq is a 4-lane crossing with
//         no gate (CDC-004)
//   r     RDC-003 (one finding for cap and gq)
//
// After `proc` each reset is a constant-leg `$mux` on `D` selected by
// `r`; after `opt_dff` it is the `SRST` pin (cap: `$sdffe`, gq: `$sdff`).
// The reset is part of the flop in both forms whatever domain drives it:
// cap's enable behind it is still the gate, and gq's `D` is still
// meta's `Q`. Both netlists (`.json`, `.optdff.json`) report the same.

module bad_bus_foreign_sync_reset (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       r_in,
    input  logic       req_in,
    output logic [3:0] cap,
    output logic [3:0] gq
);
    logic r, req;
    logic [3:0] pay, bin, gray;
    wire  [3:0] bnext = bin + 4'd1;
    always_ff @(posedge src_clk) begin
        r    <= r_in;
        req  <= req_in;
        bin  <= bnext;
        gray <= bnext ^ (bnext >> 1);
        if (req_in) pay <= pay + 4'd1;
    end

    logic en_s1, en_s2;
    always_ff @(posedge dst_clk) begin
        en_s1 <= req;
        en_s2 <= en_s1;
    end

    always_ff @(posedge dst_clk)
        if (r)          cap <= 4'd0;
        else if (en_s2) cap <= pay;

    logic [3:0] meta;
    always_ff @(posedge dst_clk) meta <= gray;
    always_ff @(posedge dst_clk)
        if (r) gq <= 4'd0;
        else   gq <= meta;
endmodule
