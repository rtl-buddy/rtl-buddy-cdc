// Residual-decline fixture for compositional boundaries (rtl-buddy-cdc#261).
//
// #261 lifts the #259 multi-clock and reconvergence declines, but not
// every shape can be summarised soundly even with the internals in hand.
// The three blocks here are the cases that must STILL decline (or still
// refuse to seed), each for a different, stated reason — a conservative
// decline carrying a `CDC-BBX` diagnostic is acceptable; silence is not.
//
//   * `oddclk` — SINGLE-clock by pin inspection (only `clk_a` is
//     recognised as a clock pin) but its second flop is clocked from
//     `strobe`, a port no clock-pin classifier accepts. Its internal
//     clk_a -> strobe crossing is REAL. Before #261 the boundary was
//     summarised as a clean single-clock block and that crossing
//     vanished with no diagnostic — pin inspection cannot see it. With
//     the internals present the flop lands in NO resolved domain, and a
//     module the pass cannot fully domain-resolve is declined outright.
//     This is a soundness catch the compositional pass ADDS.
//
//   * `twocap` — genuinely dual-clock, and its single data input `d_in`
//     is captured in BOTH `clk_a` and `clk_b`. One virtual sink cannot
//     stand for two capture domains and choosing either would drop the
//     other's crossing, so it is declined with its own message.
//
//   * `feedthru` — the positive control for the same machinery. It is
//     dual-clock and IS summarised: `d_in` is captured in `clk_a`,
//     `y` is launched from `clk_b`, and `sel` — which nothing sequential
//     captures — simply seeds no sink. There is no crossing INTO the
//     block at `sel`; whatever it gates leaves through `y`, which is
//     seeded as its own source.
//
// Every clock in the SDC is async to every other, so nothing here is
// masked by a synchronous grouping.

module oddclk (
    input  wire clk_a,
    input  wire strobe,
    input  wire d_in,
    output wire y
);
    reg q0;
    reg q1;
    always_ff @(posedge clk_a) q0 <= d_in;
    // `strobe` is not a clock pin by any classifier — the domain of q1
    // is unknown, so this crossing cannot be analysed.
    always_ff @(posedge strobe) q1 <= q0;
    assign y = q1;
endmodule

module twocap (
    input  wire clk_a,
    input  wire clk_b,
    input  wire d_in,
    output wire y_a,
    output wire y_b
);
    reg qa;
    reg qb;
    always_ff @(posedge clk_a) qa <= d_in;
    always_ff @(posedge clk_b) qb <= d_in;
    assign y_a = qa;
    assign y_b = qb;
endmodule

module feedthru (
    input  wire clk_a,
    input  wire clk_b,
    input  wire sel,
    input  wire d_in,
    output wire y
);
    reg qa;
    reg qb;
    always_ff @(posedge clk_a) qa <= d_in;
    always_ff @(posedge clk_b) qb <= qa;
    assign y = sel & qb;
endmodule

module top (
    input  wire clk_a,
    input  wire clk_b,
    input  wire strobe,
    input  wire sel,
    input  wire d_in,
    output wire y_odd,
    output wire y_two_a,
    output wire y_two_b,
    output wire y_thru
);
    oddclk u_odd (
        .clk_a (clk_a),
        .strobe(strobe),
        .d_in  (d_in),
        .y     (y_odd)
    );
    twocap u_two (
        .clk_a(clk_a),
        .clk_b(clk_b),
        .d_in (d_in),
        .y_a  (y_two_a),
        .y_b  (y_two_b)
    );
    feedthru u_thru (
        .clk_a(clk_a),
        .clk_b(clk_b),
        .sel  (sel),
        .d_in (d_in),
        .y    (y_thru)
    );
endmodule
