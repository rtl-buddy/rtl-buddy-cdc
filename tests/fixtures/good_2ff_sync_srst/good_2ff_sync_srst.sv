// Separate-flop 2FF synchroniser with a per-stage SYNCHRONOUS reset —
// the known follow-up from issue #301 (rtl-buddy-cdc#303). Same idiom
// as good_2ff_sync (`s1 <= src_q; s2 <= s1;`), but both stages live in
// an `always_ff` that also resets them to a constant on a
// destination-domain reset.
//
// After `proc` each sync reset leaves a 1-bit `$mux` in front of the
// stage's `D`: the data on one leg, the constant on the other, the
// reset on `S`. The chain walker in `_sync_chain_depth` then saw
// `s1.Q` consumed by a non-flop cell and stopped at depth 1, and
// `_chain_has_inter_stage_comb` read that same reset mux as a *gate
// between the stages*, so CDC-014 fired on every instance.
//
// Both reset polarities and both reset values are covered:
// `sync_hi` is reset active-HIGH to zero (data on the mux's A leg) and
// `sync_lo` active-LOW to one (data on the B leg). Both must stay
// silent — a synchronous reset is part of the flop, not logic on the
// path, and moves no data between the stages.
//
// The slang frontend folds the same source into `$sdff` cells whose
// `D` is the data directly (CHANGELOG #86), so it was already silent;
// this fixture is the yosys-frontend half of that parity.

module good_2ff_sync_srst (
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

    // Active-high synchronous reset to zero, both stages.
    logic sync_hi_1, sync_hi_2;
    always_ff @(posedge dst_clk) begin
        if (rst) begin
            sync_hi_1 <= 1'b0;
            sync_hi_2 <= 1'b0;
        end else begin
            sync_hi_1 <= src_q_hi;
            sync_hi_2 <= sync_hi_1;
        end
    end

    // Active-low synchronous reset to one, both stages.
    logic sync_lo_1, sync_lo_2;
    always_ff @(posedge dst_clk) begin
        if (!rst_n) begin
            sync_lo_1 <= 1'b1;
            sync_lo_2 <= 1'b1;
        end else begin
            sync_lo_1 <= src_q_lo;
            sync_lo_2 <= sync_lo_1;
        end
    end

    assign q_hi = sync_hi_2;
    assign q_lo = sync_lo_2;

endmodule
