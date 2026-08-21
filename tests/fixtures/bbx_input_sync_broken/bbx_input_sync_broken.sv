// Compositional-boundary NEGATIVE control (rtl-buddy-cdc#261).
//
// The paired counter-example to `bbx_input_sync`: two single-clock
// (clk_d) blocks that LOOK like they synchronise their boundary input
// but do not, one per way the proof must fail. Neither may be granted
// `synchronised` — `synchronised=True` is the only lever in the boundary
// model that can make the analyzer UNDER-report, so it is set only from
// a proof (architecture spec section 4.9, "soundness asymmetry").
//
//   * `oneff` — a SINGLE flop on `req_in`. Chain depth 1: a synchroniser
//     is two stages, so this is the classic metastability bug CDC-001
//     exists for.
//   * `bypass` — a proper two-stage chain (`s0` -> `s1`) that is
//     BYPASSED: `req_in` is also captured raw by `raw_q`, and the two are
//     recombined. The chain is real; the value still arrives
//     unsynchronised. Structurally, `req_in` has TWO readers, which is
//     what defeats the proof.
//
// As in `bbx_input_sync`, the parent consumes both outputs on
// UNREGISTERED ports so no parent-side flop extends a chain across the
// boundary.
//
// FLAT: CDC-001 fires twice — once on `oneff`'s lone flop, once on
// `bypass`'s raw capture.
// GREYBOXED: CDC-001 fires twice, at the two boundary input pins. Same
// rule, same severity, same count; the boundary is not trusted.

module oneff (
    input  wire clk,
    input  wire req_in,
    output wire ack_out
);
    reg q;
    // One stage only — not a synchroniser.
    always_ff @(posedge clk) q <= req_in;
    assign ack_out = q;
endmodule

module bypass (
    input  wire clk,
    input  wire req_in,
    output wire ack_out
);
    reg s0;
    reg s1;
    reg raw_q;
    // A real two-stage chain...
    always_ff @(posedge clk) s0 <= req_in;
    always_ff @(posedge clk) s1 <= s0;
    // ...bypassed by a raw capture of the same input.
    always_ff @(posedge clk) raw_q <= req_in;
    assign ack_out = s1 ^ raw_q;
endmodule

module top (
    input  wire clk_x,
    input  wire clk_d,
    input  wire din,
    output wire dout0,
    output wire dout1
);
    reg src_q;

    always_ff @(posedge clk_x) src_q <= din;

    oneff u_oneff (
        .clk    (clk_d),
        .req_in (src_q),
        .ack_out(dout0)
    );
    bypass u_bypass (
        .clk    (clk_d),
        .req_in (src_q),
        .ack_out(dout1)
    );
endmodule
