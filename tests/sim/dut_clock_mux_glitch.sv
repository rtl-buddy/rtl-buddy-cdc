// CDC-010 sim DUT: clock mux with foreign-domain select.
//
// Two same-domain clocks (ck0_a / ck0_b) feed a 2-to-1 mux whose
// select is driven by a flop in a foreign clock domain (ck1).
// Because the mux output is `sel_q ? ck0_a : ck0_b`, any sel
// transition that lands during a window where the two inputs
// disagree (in our TB they're 180° out of phase) tears the muxed
// clock — Icarus simulates this faithfully: clk_mux goes high→low→
// high or vice-versa within a sub-cycle window, producing a runt
// pulse the downstream flop will sample as a real edge.
//
// The TB counts posedges of clk_mux and compares against the
// edge count of a *safe* reference (sel pre-synchronised into the
// ck0 domain before reaching the mux). Glitchy mux output → extra
// edges → over-count vs. reference.
//
// No metastability injection on the downstream flop — the failure
// here is structural (runt clock pulse), not setup-hold.
//
// Why there's no paired "safe" RTL DUT: a combinational mux on
// async-disagreeing inputs *will* glitch on every sel transition
// regardless of whether sel is synchronised — a 2FF sync into the
// ck0 domain doesn't help, because the mux still tears whenever
// the two ck0 inputs disagree at the moment of switching. The
// actual fix is a glitch-free clock-mux primitive (vendor cell
// like BUFGMUX, or a phase-aware latch + AND-OR structure) that's
// outside what plain RTL can express. The analyzer's `cdc_sync`
// attribute on a synced sel is the user *promising* such a cell
// exists downstream; sim has no way to model that promise.

`timescale 1ns/1ps

module dut_clock_mux_glitch (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic sel_d,
    output logic [31:0] edge_count
);
    // Foreign-domain select flop.
    logic sel_q;
    always_ff @(posedge ck1) sel_q <= sel_d;

    // The bad shape: mux on a foreign-domain select.
    logic clk_mux;
    assign clk_mux = sel_q ? ck0_a : ck0_b;

    // Counter clocked by the muxed clock. Every posedge increments.
    initial edge_count = '0;
    always_ff @(posedge clk_mux) edge_count <= edge_count + 1;
endmodule
