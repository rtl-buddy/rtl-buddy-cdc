// Site-registered CDC primitive via `--sync-primitive` (rtl-buddy-cdc#275).
//
// The XPM family is recognised built-in, but plenty of shops wrap their
// own blessed synchroniser (or use another vendor's macro). `acme_cdc_sync`
// is that shape: a dual-clock blackbox that is a correct synchroniser,
// which the analyzer has no way to know about a priori.
//
// Without `--sync-primitive acme_cdc_sync` this design reports a CDC-BBX
// error (dual-clock blackbox, not provably single-clock). With it, the
// instance is summarised at its `dest_clk` domain and the design is clean.
//
// Note the deliberate difference from XPM: `port_domain` only honours the
// `src_*` / `dest_*` convention for the XPM family, so a registered
// primitive's outputs are ALL attributed to the destination clock — the
// conservative reading, since we have no promise about its port naming.

(* blackbox *)
module acme_cdc_sync #(
    parameter integer DEST_SYNC_FF = 3
) (
    input  wire src_clk,
    input  wire src_in,
    input  wire dest_clk,
    output wire dest_out
);
endmodule

module sync_primitive_optin (
    input  wire clk_a,
    input  wire clk_b,
    input  wire flag_in,
    output wire flag_out
);
    reg flag_q;
    always_ff @(posedge clk_a) flag_q <= flag_in;

    wire flag_sync;
    acme_cdc_sync #(
        .DEST_SYNC_FF(3)
    ) u_acme (
        .src_clk (clk_a),
        .src_in  (flag_q),
        .dest_clk(clk_b),
        .dest_out(flag_sync)
    );

    reg flag_b;
    always_ff @(posedge clk_b) flag_b <= flag_sync;
    assign flag_out = flag_b;
endmodule
