// Analyse-once lifting fixture (rtl-buddy-cdc#261).
//
// `xsync` is a genuinely DUAL-clock IP: it captures `d_in` in `clk_s`,
// hands the value across to `clk_c` through a two-stage synchroniser,
// and launches `d_out` from the chain's tail. Before #261 such a block
// was DECLINED outright — two clock roots meant "not provably
// single-clock", so every instance became an opaque CDC-BBX error and
// the internal clk_s -> clk_c crossing was never analysed at all.
//
// It is instantiated TWICE, in the SAME clock context. That is the
// compositional contract the epic is about: the module is analysed ONCE
// (the summary cache keys on `(module type, clock-pin -> root mapping)`)
// and its internal finding is lifted into the parent's report ONCE,
// naming both instances — not once per instance, and not zero times.
//
// The internal chain is exactly 2 deep, so it is clean at the default
// `--sync-depth 2` and raises CDC-002 at `--sync-depth 3`. The top level
// itself has no crossings at all: `d0` / `d1` are typed into clk_s and
// the outputs go nowhere, so every finding in this design comes from
// inside the boundary.
//
// FLAT (`--sync-depth 3`): CDC-002 fires TWICE, once per inlined copy.
// GREYBOXED (`--sync-depth 3`): CDC-002 fires ONCE, attributed to
// `u_a, u_b`. Same hazard, reported once — and no CDC-BBX.

module xsync (
    input  wire clk_s,
    input  wire clk_c,
    input  wire d_in,
    output wire d_out
);
    reg src_q;
    reg s0;
    reg s1;
    // Source domain.
    always_ff @(posedge clk_s) src_q <= d_in;
    // The internal clk_s -> clk_c crossing, behind a 2-flop chain.
    always_ff @(posedge clk_c) s0 <= src_q;
    always_ff @(posedge clk_c) s1 <= s0;
    assign d_out = s1;
endmodule

module top (
    input  wire clk_s,
    input  wire clk_c,
    input  wire d0,
    input  wire d1,
    output wire q0,
    output wire q1
);
    xsync u_a (
        .clk_s(clk_s),
        .clk_c(clk_c),
        .d_in (d0),
        .d_out(q0)
    );
    xsync u_b (
        .clk_s(clk_s),
        .clk_c(clk_c),
        .d_in (d1),
        .d_out(q1)
    );
endmodule
