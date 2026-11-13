// Positive counterpart of bad_exported_sync_stage: a textbook two-flop
// synchroniser whose *last* stage is exported straight to an output
// port and also feeds a two-register output pipeline. Must be clean.
//
// The first stage `dst_meta` has exactly one reader (`dst_sync`), so
// the chain is two flops deep. Exporting the last stage is the normal
// way to use a synchronised value, and the tap ends the chain there:
// the pipeline registers behind it are not extra synchroniser stages,
// so the four same-domain flops in a row are not a cascade (no CDC-018).
//
//        src_clk          dst_clk
//   [ src_q ] ─► [ dst_meta ] ─► [ dst_sync ] ─► [ pipe0 ] ─► [ q_pipe ]
//                                     └─────────────────────► q_tap
// Issue: rtl-buddy-cdc#344

module good_exported_sync_stage (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_tap,
    output logic q_pipe
);

    logic src_q;
    logic dst_meta;
    logic dst_sync;
    logic pipe0;

    always_ff @(posedge src_clk) src_q <= d_in;

    always_ff @(posedge dst_clk) begin
        dst_meta <= src_q;
        dst_sync <= dst_meta;
    end
    assign q_tap = dst_sync;

    // Output pipeline on the synchronised value.
    always_ff @(posedge dst_clk) begin
        pipe0  <= dst_sync;
        q_pipe <= pipe0;
    end

endmodule
