// P2 auto-abstract safety fixture: foreign-domain INPUT into a
// single-clock subtree must NOT be abstracted away (#256).
//
// `top` runs two async clocks, clk_a and clk_b. The leaf `pipe` is a
// pure clk_a pipeline (two clk_a stages) — a SINGLE-CLOCK subtree by
// the clock-pin test. BUT here its `d_in` is driven by a clk_b parent
// flop (`src_q`), so the real async crossing (clk_b -> clk_a) lands on
// pipe's FIRST internal flop, *inside* the subtree's input boundary.
//
// If P2 abstracted `pipe` to its output port boundary it would seed
// only an output-side virtual source (clk_a) and NO input-side virtual
// sink, so the clk_b -> clk_a input crossing would silently vanish —
// the flattened design reports it, the abstracted design would not.
//
// The safety property: because a foreign-domain signal enters the
// subtree's data input, P2 must REFUSE to abstract `pipe` (until P3's
// dst_boundary input-sink seeding lands). The FLATTENED design and the
// "auto-abstract candidate" (pipe blackboxed) design must therefore
// produce IDENTICAL violations and identical summary.* counts: the
// blackboxed run declines the abstraction and the crossing is
// preserved, NOT dropped.

module pipe (
    input  wire       clk,
    input  wire [3:0] d_in,
    output wire [3:0] d_out
);
    reg [3:0] s0;
    reg [3:0] s1;
    always_ff @(posedge clk) s0 <= d_in;
    always_ff @(posedge clk) s1 <= s0;
    assign d_out = s1;
endmodule

module top (
    input  wire       clk_a,
    input  wire       clk_b,
    input  wire [3:0] d_in,
    output wire [3:0] q_out
);
    reg [3:0] src_q;
    reg [3:0] dst_q;

    // clk_b source register feeding the clk_a single-clock subtree —
    // a FOREIGN-domain data input (the async crossing clk_b -> clk_a).
    always_ff @(posedge clk_b) src_q <= d_in;

    wire [3:0] pipe_q;
    pipe u_pipe (
        .clk  (clk_a),
        .d_in (src_q),
        .d_out(pipe_q)
    );

    // clk_a capture of the clk_a subtree output — same domain, no
    // crossing out.
    always_ff @(posedge clk_a) dst_q <= pipe_q;

    assign q_out = dst_q;
endmodule
