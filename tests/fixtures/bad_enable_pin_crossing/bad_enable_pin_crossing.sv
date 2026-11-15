// A clock-domain crossing that enters a flop only through its enable
// (rtl-buddy-cdc#375).
//
// `en_a` is registered in `clk_a` and used, unsynchronised, as the load
// enable of `q` in `clk_b`. The data `d_b` is a `clk_b` input, so the
// only crossing is the enable: when `en_a` changes close to a `clk_b`
// edge, `q` can go metastable or load on the wrong cycle.
//
// After `proc` the enable is a hold mux on `q`'s D; after `opt_dff` it
// is folded into a `$dffe` EN pin (`$_DFFE_*` E at gate level). The
// crossing, and CDC-001 on it, must be the same in all three forms.

module bad_enable_pin_crossing (
    input  logic clk_a,
    input  logic clk_b,
    input  logic go,
    input  logic d_b,
    output logic q
);

    logic en_a;
    always_ff @(posedge clk_a) en_a <= go;

    always_ff @(posedge clk_b) if (en_a) q <= d_b;

endmodule
