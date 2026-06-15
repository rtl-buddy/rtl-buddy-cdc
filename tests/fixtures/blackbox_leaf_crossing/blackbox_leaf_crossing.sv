// P1 blackbox boundary fixture (#255).
//
// `top` drives a sub-block `leaf` from a clk_a flop and captures
// `leaf`'s output into a clk_b flop, so the data path runs THROUGH the
// blackbox: src_q (clk_a) -> leaf.d_in -> leaf.d_out -> dst_q (clk_b).
//
// When `leaf` is blackboxed via `read_slang --blackboxed-module leaf`,
// it survives flatten with its real name + an `attributes.blackbox`
// flag and zero internals; the parent keeps `u_leaf` as an ordinary
// cell whose `type` is `leaf`. This is the boundary-cell topology
// netlist.load must accept (the single-module-after-flatten invariant
// is relaxed to "one top + N blackbox siblings").
//
// `leaf` exposes a data-only port boundary (no clock pin) so the P0
// boundary summariser (P2) can describe it purely by its data ports;
// P1 only proves the boundary loads and the analyzer runs over it.

module leaf (
    input  wire [3:0] d_in,
    output wire [3:0] d_out
);
    // Internals are irrelevant once blackboxed; a plain registered-less
    // recode keeps the un-blackboxed elaboration legal too.
    assign d_out = d_in ^ 4'hA;
endmodule

module top (
    input  wire       clk_a,
    input  wire       clk_b,
    input  wire [3:0] d_in,
    output wire [3:0] q_out
);
    reg [3:0] src_q;
    reg [3:0] dst_q;

    always_ff @(posedge clk_a) src_q <= d_in;

    wire [3:0] leaf_q;
    leaf u_leaf (
        .d_in (src_q),
        .d_out(leaf_q)
    );

    always_ff @(posedge clk_b) dst_q <= leaf_q;

    assign q_out = dst_q;
endmodule
