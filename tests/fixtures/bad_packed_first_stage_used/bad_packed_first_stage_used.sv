// Adversarial counterpart to good_packed_shift_sync, pinning the
// reader-count guard in the packed-shift-register recognizer (#264).
//
// The destination is again a 2-bit shifting register, but the *first*
// stage (`sync_sr[0]`) is consumed combinationally — the potentially
// metastable Q[0] is in use after a single destination flop. The
// effective synchronizer depth is therefore 1, and CDC-001 must still
// fire even though the register is multi-bit and self-shifting.
//
// This is the over-suppression trap: the packed-shift fast path must
// not blanket-accept any self-shifting multi-bit flop as a deep
// synchronizer. The walk only extends a stage whose output feeds
// exactly the next shift lane (reader count == 1); tapping the first
// stage breaks that and the chain ends at depth 1.

module bad_packed_first_stage_used (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    logic [1:0] sync_sr;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_sr <= 2'b0;
        else        sync_sr <= {sync_sr[0], src_q};
    end

    // First-stage value tapped through combinational logic: the
    // synchronized value is consumed after one flop, so this is a
    // depth-1 crossing despite the wider register.
    assign q_out = ~sync_sr[0];

endmodule
