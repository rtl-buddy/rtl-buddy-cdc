// Clock-output blackbox soundness fixture (rtl-buddy-cdc#273).
//
// `clkfwd_tile` is SINGLE-CLOCK internally, so the pre-#273 boundary
// check happily accepted it as a `--blackbox` candidate. But it also
// has a CLOCK OUTPUT: `clk_out` forwards `clk_in` onward through
// `clk_buf`, and the next tile is clocked from it. Blackboxing the tile
// therefore elides the whole clock generation/forwarding network — the
// forwarded clock leaves an opaque boundary output, its downstream
// consumers go domain-unknown (or vanish with them), and the cells
// feeding the boundary lose their clock-distribution status.
//
// `datapath_core` is the positive control: single-clock, data-only
// outputs. It must stay ACCEPTED (abstracted) — the clock-output
// decline must not over-fire on the common case.
//
// FLAT: clean — `clk_out` is a buffered copy of `clk`, both tiles are in
// the one `clk` domain.
// BLACKBOXED (`--blackbox clkfwd_tile --blackbox datapath_core`): both
// `clkfwd_tile` instances are DECLINED with the clock-output flavour of
// CDC-BBX (the module-level verdict covers `u1`, whose own `clk_out` is
// unconnected), while `u_core` is still abstracted.

module clk_buf (
    input  wire i,
    output wire o
);
    assign o = i;
endmodule

module clkfwd_tile (
    input  wire clk_in,
    output wire clk_out,
    input  wire d,
    output wire q
);
    // Forwards the clock onward — the reason this module must not be
    // blackboxed.
    clk_buf u_buf (
        .i(clk_in),
        .o(clk_out)
    );
    reg q_r;
    always_ff @(posedge clk_in) q_r <= d;
    assign q = q_r;
endmodule

module datapath_core (
    input  wire       clk,
    input  wire [3:0] d,
    output wire [3:0] q
);
    reg [3:0] s0;
    always_ff @(posedge clk) s0 <= d;
    assign q = s0;
endmodule

module top (
    input  wire       clk,
    input  wire       d,
    input  wire [3:0] din,
    output wire       q0,
    output wire       q1,
    output wire [3:0] dout
);
    wire clk1;
    wire q0i;

    // u0 drives the forwarded clock; u1 is clocked FROM it.
    clkfwd_tile u0 (
        .clk_in (clk),
        .clk_out(clk1),
        .d      (d),
        .q      (q0i)
    );
    clkfwd_tile u1 (
        .clk_in (clk1),
        .clk_out(),
        .d      (q0i),
        .q      (q1)
    );
    assign q0 = q0i;

    // Positive control: data-only outputs, must stay abstracted.
    datapath_core u_core (
        .clk(clk),
        .d  (din),
        .q  (dout)
    );
endmodule
