// Negative-case fixture: a one-flop "synchroniser" whose stage is
// exported straight to an output port and also pipelined. Should trip
// CDC-001.
//
// `dst_meta` is the only destination flop that samples the crossing.
// Its possibly-metastable value leaves the design on `q_tap` with no
// logic in between, and also feeds `q_pipe`. The exported tap is a
// second reader of the first stage, so `dst_meta -> q_pipe` is not a
// two-stage synchroniser: the chain is one flop deep.
//
//        src_clk          dst_clk
//   [ src_q ] ──────► [ dst_meta ] ──► [ q_pipe ] ──► q_pipe
//                          └────────────────────────► q_tap  (direct)
// Issue: rtl-buddy-cdc#344

module bad_exported_sync_stage (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_tap,
    output logic q_pipe
);

    logic src_q;
    logic dst_meta;

    always_ff @(posedge src_clk) src_q <= d_in;

    // BAD: the first (metastable) stage is used directly.
    always_ff @(posedge dst_clk) dst_meta <= src_q;
    assign q_tap = dst_meta;

    always_ff @(posedge dst_clk) q_pipe <= dst_meta;

endmodule
