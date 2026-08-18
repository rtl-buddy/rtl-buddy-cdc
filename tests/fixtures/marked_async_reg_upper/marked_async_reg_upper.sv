// Uppercase `ASYNC_REG` recognition (rtl-buddy-cdc#275).
//
// `USER_SYNC_ATTRS` has carried an `async_reg` alias since the marker
// plumbing landed — the point being to honour the Xilinx synthesis
// attribute that pins a synchroniser's stages into adjacent slices.
// But Xilinx spells it `(* ASYNC_REG = "TRUE" *)`, and Yosys preserves
// the attribute name verbatim, so a case-sensitive match never fired on
// the one idiom the alias existed for. The XPM CDC macro sources tag
// their internal stages exactly this way, so the same gap also closed
// the "user put the XPM sources in the filelist" route.
//
// This fixture is a SINGLE-stage crossing — structurally a CDC-001 —
// held silent purely by the uppercase attribute. If the case-fold
// regresses, CDC-001 fires and the test fails loudly. (The lowercase
// spelling is covered by marked_single_ff_sync; this fixture is the
// case-sensitivity twin.)

module marked_async_reg_upper (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    // The Xilinx spelling: uppercase, string-valued.
    (* ASYNC_REG = "TRUE" *) logic dst_meta;
    always_ff @(posedge dst_clk) dst_meta <= src_q;

    assign q_out = dst_meta;

endmodule
