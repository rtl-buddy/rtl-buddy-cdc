// declared_pin_clock — an internal generated clock the user DID declare,
// with a plain `create_clock` aimed at the pin (issue #270).
//
// Same shape as `inferred_fwd_clock`: `clk_div` is a divide-by-2 toggle
// flop on `clk_a` whose Q clocks a four-flop bank, so the net clears the
// advisory's >=4 CLK-pin fanout floor. The difference is the SDC, which
// declares that net directly:
//
//   create_clock -name div_clk -period 20.0 [get_pins clk_div]
//
// That is a declaration, not a `create_generated_clock`, so the target
// lands in `Clock.ports` (reachable via `ClockSpec.clock_for_port`) and
// never in `ClockSpec.pin_clocks`. Before #270 the advisory consulted
// only `pin_clocks` plus the top-level input ports, so it re-reported a
// clock the user had already written down. `clk_div` must NOT appear in
// `inferred_clock_candidates`.
//
// Advisory-only, as always: the bank flops resolve to `clk_a` through
// the divider trace either way, so no domain, crossing, or violation
// moves — this fixture pins the false positive, nothing else.
//
// Parity anchor: `q_a` (clk_a) drives `q_b` (clk_b) directly across an
// async clock boundary — a genuine clk_a->clk_b crossing.
//
// Build:
//   yosys -p 'read_verilog -sv declared_pin_clock.sv;
//             hierarchy -top declared_pin_clock;
//             proc; flatten;
//             write_json declared_pin_clock.json'
module declared_pin_clock (
    input  wire clk_a,
    input  wire clk_b,
    input  wire rst_n,
    input  wire din,
    output wire dout
);
    // Divide-by-2: toggle flop forms an internal clock — declared in the
    // SDC with create_clock on the pin.
    reg clk_div;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) clk_div <= 1'b0;
        else        clk_div <= ~clk_div;

    // Bank clocked by the declared internal clock. Each bit is its own
    // single-bit flop (distinct always_ff blocks) so the synthesised
    // netlist keeps four separate FF cells sharing the clk_div net on
    // their CLK pin — the >=4 fanout the advisory keys on.
    reg b0, b1, b2, b3;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b0 <= 1'b0; else b0 <= din;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b1 <= 1'b0; else b1 <= b0;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b2 <= 1'b0; else b2 <= b1;
    always_ff @(posedge clk_div or negedge rst_n) if (!rst_n) b3 <= 1'b0; else b3 <= b2;
    wire [3:0] bank = {b3, b2, b1, b0};

    // Parity: a real clk_a -> clk_b async crossing.
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
