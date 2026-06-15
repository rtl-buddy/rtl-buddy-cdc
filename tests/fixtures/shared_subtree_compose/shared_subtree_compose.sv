// shared_subtree_compose — P3 (#257) hierarchical / compositional parity.
//
// One single-clock subtree (`pipe`, clk_a only) instantiated TWICE
// (`u_pipe0`, `u_pipe1`). Both instances feed clk_b destination flops,
// so each carries an async clk_a -> clk_b crossing at its output.
//
// The whole point of phase 3: the abstracted run summarises `pipe`
// ONCE (cache by module identity) and re-applies that boundary to both
// instances, yet produces the SAME violations as the flattened run that
// inlines both copies. The destination flops (`dst_q0` / `dst_q1`) are
// real top-level flops present in both views, so the CDC-004 anchors
// match cell-for-cell — parity.
module pipe (
    input        clk,
    input  [3:0] d_in,
    output [3:0] d_out
);
    reg [3:0] s0;
    always_ff @(posedge clk) s0 <= d_in;
    assign d_out = s0;
endmodule

module shared_subtree_compose (
    input        clk_a,
    input        clk_b,
    input  [3:0] d_in,
    output [7:0] q_out
);
    reg  [3:0] src_q;
    wire [3:0] pipe0_q, pipe1_q;
    reg  [3:0] dst_q0, dst_q1;

    always_ff @(posedge clk_a) src_q <= d_in;

    pipe u_pipe0 (.clk(clk_a), .d_in(src_q), .d_out(pipe0_q));
    pipe u_pipe1 (.clk(clk_a), .d_in(src_q), .d_out(pipe1_q));

    always_ff @(posedge clk_b) dst_q0 <= pipe0_q;
    always_ff @(posedge clk_b) dst_q1 <= pipe1_q;

    assign q_out = {dst_q1, dst_q0};
endmodule
