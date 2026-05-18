// SV-attribute coverage fixture: (* reset_sync *) marks a flop as a
// user-vetted reset-synchroniser stage even when the structural
// recogniser wouldn't match.
//
// The chain head's D pin is fed by `upstream_rst_n` (a port), not a
// constant — the structural detector in
// :func:`rtl_buddy_cdc.reset_domain.find_reset_synchronizers`
// deliberately requires a constant-fed head, so it would normally
// NOT classify these flops as a synchroniser. The `(* reset_sync *)`
// annotation overrides that decision.
//
// Without the attribute, RDC-002 would fire on the consumer flop
// (`q_out`) because the producer's ARST_VALUE (=0) doesn't match
// q_out's ARST_POLARITY (=1). With the attribute marking the second
// stage of the would-be sync as recognised, RDC-002 skips q_out and
// no rule fires.

module marked_reset_sync (
    input  logic clk,
    input  logic upstream_rst_n,
    input  logic d_in,
    output logic q_out
);

    logic rst_meta;
    always_ff @(posedge clk or negedge upstream_rst_n) begin
        if (!upstream_rst_n) rst_meta <= 1'b0;
        else                 rst_meta <= upstream_rst_n;  // not constant — D=port
    end

    // (* reset_sync *) marks this flop as a vetted sync stage even
    // though the chain head's D isn't a constant.
    (* reset_sync *) logic rst_sync;
    always_ff @(posedge clk or negedge upstream_rst_n) begin
        if (!upstream_rst_n) rst_sync <= 1'b0;
        else                 rst_sync <= rst_meta;
    end

    // Consumer with deliberately mismatched polarity — would normally
    // trigger RDC-002, but the (* reset_sync *) on `rst_sync` makes
    // it count as a synchroniser stage and the rule skips q_out.
    always_ff @(posedge clk or posedge rst_sync) begin
        if (rst_sync) q_out <= 1'b0;
        else          q_out <= d_in;
    end

endmodule
