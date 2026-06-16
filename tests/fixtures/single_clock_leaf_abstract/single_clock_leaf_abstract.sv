// P2 auto-abstract single-clock-subtree fixture (#256).
//
// `top` runs two async clocks, clk_a and clk_b. A leaf sub-block,
// `pipe`, is a pure clk_a pipeline (two clk_a stages, no second clock)
// — a SINGLE-CLOCK subtree that carries no internal crossing. Its
// input comes from a clk_a parent flop (same domain, no crossing in)
// and its output is captured by a clk_b parent flop (the one real
// async crossing, clk_a -> clk_b).
//
// The safety property P2 must preserve: analysing the FLATTENED design
// (pipe inlined, the crossing is src_q->...->dst_q flop-to-flop) and
// analysing the AUTO-ABSTRACTED design (pipe blackboxed, summarised to
// its port boundary so the crossing is re-seeded from pipe.d_out as a
// virtual source) must produce identical violations and identical
// summary.* counts. The abstracted design has fewer flops to walk
// (pipe's two internal stages are gone), which is the scaling win.

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

    // clk_a source register feeding the single-clock subtree.
    always_ff @(posedge clk_a) src_q <= d_in;

    wire [3:0] pipe_q;
    pipe u_pipe (
        .clk  (clk_a),
        .d_in (src_q),
        .d_out(pipe_q)
    );

    // clk_b capture of the clk_a subtree output — the async crossing.
    always_ff @(posedge clk_b) dst_q <= pipe_q;

    assign q_out = dst_q;
endmodule
