// Parity anchor for rtl-buddy-cdc#272 — the same untyped sync reset,
// lowered the *other* way.
//
// Byte-for-byte the RTL and SDC of `bad_untyped_sync_reset_srst`, but
// built without `opt_dff`, so Yosys keeps a plain `$dff` whose `D` is
// a `$mux` selecting between `1'b0` and `~qr` on `srst`. The reset
// therefore arrives on a `D` pin and the ordinary port walk in
// `find_crossings` sees it.
//
// This shape was always reported. It is checked in so the pair pins
// the invariant the issue asked for: a missing `set_input_delay
// -clock` on a synchronous reset must produce the *same* CDC-011
// finding — same rule id, same severity, same message — whichever
// flop cell the synthesis pass infers.
//
// CDC-011 must fire twice at `warning` severity (`srst`, `dctl`) and
// no other rule may fire.
//
// Build:
//   yosys -p 'read_verilog -sv bad_untyped_sync_reset_mux.sv;
//             hierarchy -top bad_untyped_sync_reset_mux;
//             proc; flatten; opt_clean;
//             write_json bad_untyped_sync_reset_mux.json'
//
// No `opt_dff` — that is the whole point of this fixture.

module bad_untyped_sync_reset_mux (
    input  logic clk,
    input  logic srst,
    input  logic dctl,
    output logic qr,
    output logic qd
);

    // Same source as the `_srst` fixture; without `opt_dff` the reset
    // stays a `$mux` on the `$dff`'s `D` pin.
    always_ff @(posedge clk) begin
        if (srst) qr <= 1'b0;
        else      qr <= ~qr;
    end

    always_ff @(posedge clk) qd <= dctl;

endmodule
