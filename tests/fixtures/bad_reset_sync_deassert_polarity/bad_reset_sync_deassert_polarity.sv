// Negative-case fixture for RDC-007.
//
// A reset-synchroniser chain whose head D is tied to the *asserted*
// polarity instead of the deasserted one. The chain is structurally
// well-formed by every other check (constant-fed head, same-domain
// Q→D walk, shared async reset, ≥2 flops) so
// `find_reset_synchronizers` accepts it — but its tail's Q reloads
// the asserted value (1'b0) on every deassertion edge, so the
// synchronised reset never propagates "out of reset" to downstream
// consumers.
//
// RDC-007 must fire exactly once (on the chain tail). Other RDC
// rules must stay silent — the chain head's reset is a top-level
// port and there's no other domain in the design.

module bad_reset_sync_deassert_polarity (
    input  logic dst_clk,
    input  logic raw_rst_n,
    output logic rst_n_sync
);

    // Active-low reset chain. Deassertion edge should load 1'b1
    // (so the chain's Q rises after raw_rst_n releases). This chain
    // loads 1'b0 — a one-shot stuck driving 0 forever.
    logic meta;
    always_ff @(posedge dst_clk or negedge raw_rst_n) begin
        if (!raw_rst_n) meta       <= 1'b0;
        else            meta       <= 1'b0;   // BUG: should be 1'b1
    end
    always_ff @(posedge dst_clk or negedge raw_rst_n) begin
        if (!raw_rst_n) rst_n_sync <= 1'b0;
        else            rst_n_sync <= meta;
    end

endmodule
