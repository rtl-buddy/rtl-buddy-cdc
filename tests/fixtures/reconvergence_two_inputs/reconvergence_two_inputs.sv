// Reconvergence-unsafe blackbox fixture (rtl-buddy-cdc#259 audit, FIX 3).
//
// `recon` is a SINGLE-CLOCK (clk_d) block with TWO separate inputs,
// `a_in` and `b_in`. ONE foreign-domain (clk_x) source register in the
// parent fans out to BOTH inputs; each input passes through an
// independent two-flop synchroniser chain inside `recon`, and the two
// chain outputs RECONVERGE (a2 ^ b2) into one clk_d register. A single
// source fanning out to two independent sync chains that recombine is
// the textbook CDC-005 reconvergence hazard.
//
// The bug FIX 3 closes: a single-clock block that IS abstracted but has
// >=2 distinct foreign-domain crossings entering DISTINCT input ports
// can hide that internal reconvergence — the boundary star-collapse
// severs the internal graph, so the reconvergence among those ports
// cannot be checked at the boundary. Conservative policy: REFUSE to
// abstract such a block.
//
// FLAT: CDC-005 reconvergence fires.
// BLACKBOXED: reconvergence-unsafe — the linter SKIPS blackboxing (the
// block is refused / opaque) and emits the reconvergence diagnostic; it
// does NOT report the block as clean.

module recon (
    input  wire clk,
    input  wire a_in,
    input  wire b_in,
    output wire y_out
);
    // Two independent 2-flop synchroniser chains, one per input.
    reg a1, a2;
    reg b1, b2;
    reg y_q;
    always_ff @(posedge clk) a1 <= a_in;
    always_ff @(posedge clk) a2 <= a1;
    always_ff @(posedge clk) b1 <= b_in;
    always_ff @(posedge clk) b2 <= b1;
    // Reconvergence: the two synchronised outputs recombine.
    always_ff @(posedge clk) y_q <= a2 ^ b2;
    assign y_out = y_q;
endmodule

module top (
    input  wire clk_x,
    input  wire clk_d,
    input  wire din,
    output wire dout
);
    reg src_q;
    reg dst_q;

    // ONE clk_x source register fans out to BOTH foreign inputs.
    always_ff @(posedge clk_x) src_q <= din;

    wire recon_y;
    recon u_recon (
        .clk  (clk_d),
        .a_in (src_q),
        .b_in (src_q),
        .y_out(recon_y)
    );

    always_ff @(posedge clk_d) dst_q <= recon_y;
    assign dout = dst_q;
endmodule
