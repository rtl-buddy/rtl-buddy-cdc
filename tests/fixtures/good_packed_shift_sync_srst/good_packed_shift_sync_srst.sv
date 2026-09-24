// Packed-shift-register synchronizer with a SYNCHRONOUS reset — the
// false-positive case from issue #301, the #264 follow-up. Same idiom
// as good_packed_shift_sync (`sr <= {sr[0], src_q}`, tapped from the
// top bit), but the shift lives in an `always_ff` that also resets the
// register to a constant on a destination-domain reset.
//
// After `proc` the sync reset leaves a multi-bit `$mux` in front of the
// flop's `D`: the shift vector on one leg, the constant reset value on
// the other, and the reset on `S`. `D` is then no longer lane-for-lane
// the flop's own `Q` bits, so the packed recogniser stopped matching
// and CDC-001 fired a false "chain depth = 1" on every instance.
//
// Both reset polarities and both reset values are covered:
// `sync_hi` is reset active-HIGH to all-zeros (shift vector on the
// mux's A leg) and `sync_lo` is reset active-LOW to all-ones (shift
// vector on the B leg). Both must stay silent — the reset only forces
// the lanes to a constant, it moves no data between them, so the
// synchroniser is still two stages deep.
//
// The slang frontend folds the same source into an `$sdff` whose `D`
// is the shift vector directly (CHANGELOG #86), so it was already
// silent; this fixture is the yosys-frontend half of that parity.

module good_packed_shift_sync_srst (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst,
    input  logic rst_n,
    input  logic d_hi,
    input  logic d_lo,
    output logic q_hi,
    output logic q_lo
);

    logic src_q_hi;
    always_ff @(posedge src_clk) src_q_hi <= d_hi;

    logic src_q_lo;
    always_ff @(posedge src_clk) src_q_lo <= d_lo;

    // Active-high synchronous reset to all-zeros.
    logic [1:0] sync_hi;
    always_ff @(posedge dst_clk) begin
        if (rst) sync_hi <= 2'b00;
        else     sync_hi <= {sync_hi[0], src_q_hi};
    end

    // Active-low synchronous reset to all-ones.
    logic [1:0] sync_lo;
    always_ff @(posedge dst_clk) begin
        if (!rst_n) sync_lo <= 2'b11;
        else        sync_lo <= {sync_lo[0], src_q_lo};
    end

    assign q_hi = sync_hi[1];
    assign q_lo = sync_lo[1];

endmodule
