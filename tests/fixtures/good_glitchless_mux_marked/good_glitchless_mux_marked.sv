// Positive counterpart to bad_async_clock_mux.
//
// Same simple 2:1 clock mux with a foreign-domain select, but the
// user marks the select wire with (* glitchless_clock_mux *) to
// vouch that the surrounding mux topology is glitch-free (e.g. an
// external cross-coupled-latch envelope or a foundry library cell
// that handles the safe handoff). The "synchronise the select"
// fix CDC-010 normally proposes would break a correctly-built
// glitchless mux by introducing a single-clock dependency that
// defeats the other-clock-aware gating.
//
// CDC-010 must stay silent on the marked select. The downstream
// flop driven by the muxed clock is otherwise identical to
// bad_async_clock_mux.

module good_glitchless_mux_marked (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);

    (* glitchless_clock_mux *) logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    logic ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;

endmodule
