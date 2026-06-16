// inferred_fwd_clock — an undeclared internal generated clock (P3/#263).
//
// `clk_div` is a divide-by-2 toggle flop on `clk_a`: its Q is wired
// directly into the CLK pin of a four-flop bank. The SDC declares only
// `clk_a` and `clk_b` — there is NO `create_generated_clock` for
// `clk_div`, so the user "forgot to declare" the forwarded/divided
// clock. The P3 advisory flags `clk_div`'s driver as an inferred-clock
// candidate (net drives >=4 flop CLK pins, not in the SDC).
//
// The advisory is ADVISORY ONLY. The four bank flops still resolve to
// `clk_a` because `trace_clock_root` already follows a divider flop's Q
// back to its own CLK — a real clock-root trace, independent of the
// fanout heuristic. So the candidate report changes no domain and no
// crossing.
//
// Parity anchor: `q_a` (clk_a) drives `q_b` (clk_b) directly across an
// async clock boundary — a genuine clk_a->clk_b crossing that must be
// reported identically with or without the advisory feature.
module inferred_fwd_clock (
    input  wire clk_a,
    input  wire clk_b,
    input  wire rst_n,
    input  wire din,
    output wire dout
);
    // Divide-by-2: toggle flop forms an internal (undeclared) clock.
    reg clk_div;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) clk_div <= 1'b0;
        else        clk_div <= ~clk_div;

    // Bank clocked by the undeclared internal clock. Each bit is its own
    // single-bit flop (distinct always_ff blocks) so the synthesised
    // netlist keeps four separate FF cells all sharing the clk_div net on
    // their CLK pin — the >=4 CLK-pin fanout the advisory keys on. They
    // resolve to clk_a via the divider trace; the advisory still flags
    // clk_div as a forgotten create_generated_clock target.
    reg b0, b1, b2, b3;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b0 <= 1'b0; else b0 <= din;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b1 <= 1'b0; else b1 <= b0;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b2 <= 1'b0; else b2 <= b1;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b3 <= 1'b0; else b3 <= b2;
    wire [3:0] bank = {b3, b2, b1, b0};

    // Parity: a real clk_a -> clk_b async crossing, untouched by P3.
    reg q_a;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) q_a <= 1'b0;
        else        q_a <= din;

    reg q_b;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) q_b <= 1'b0;
        else        q_b <= q_a;

    assign dout = bank[3] ^ q_b;
endmodule
