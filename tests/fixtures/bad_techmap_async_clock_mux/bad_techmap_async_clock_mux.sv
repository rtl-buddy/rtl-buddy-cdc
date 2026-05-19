// CDC-010 phase-3 fixture: the same clock-mux failure as
// ``bad_async_clock_mux``, but built with ``simplemap t:$mux``
// applied at fixture-build time so the high-level ``$mux`` lowers
// to a gate-level ``$_MUX_`` cell. Exercises the post-tech-mapping
// path through ``_control_pins_for`` — the rule must still find
// the ``$_MUX_.S`` control pin and fire.
//
// The flops stay as ``$adff`` (we don't lower them: ``find_flops``
// only recognises the higher-level FF cell types, and dropping
// them to ``$_DFF_*`` would defeat the test by hiding the
// foreign-domain source flop from domain assignment).

module bad_techmap_async_clock_mux (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);

    // Select is in ck1's domain — the foreign-domain source.
    logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    // Clock mux — after ``simplemap`` this is a gate-level
    // ``$_MUX_`` with S / A / B / Y pins, identical to ``$mux``
    // semantically but with a different cell type that only the
    // phase-3 entry in the explicit map recognises.
    logic ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;

endmodule
