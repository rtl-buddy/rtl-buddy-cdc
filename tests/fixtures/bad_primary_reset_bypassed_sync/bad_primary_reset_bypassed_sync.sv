// RDC-008 in both of its shapes (rtl-buddy-cdc#398).
//
// The reset port rst_n has a 2FF reset synchroniser in clk_a
// (sync_meta -> sync_rst_n), so the design knows the port needs one.
//
//   clk_a  a_ok resets from the synchroniser's tail; a_raw0 / a_raw1
//          take the raw port instead and bypass the synchroniser this
//          domain already has. RDC-008 names its tail.
//   clk_b  b_raw0 / b_raw1 take the raw port and clk_b has no
//          synchroniser for it. RDC-008 asks for one.
//
// Either way the deassertion edge reaches those flops unsynchronised
// to their clock. RDC-008 must fire once per clock and nothing else.

module bad_primary_reset_bypassed_sync (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_b,
    output logic q_a,
    output logic q_b
);

    // 2FF reset synchroniser for rst_n in clk_a.
    logic sync_meta, sync_rst_n;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= 1'b1;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) sync_rst_n <= 1'b0;
        else        sync_rst_n <= sync_meta;

    // clk_a: one flop through the synchroniser, two around it.
    logic a_ok, a_raw0, a_raw1;
    always_ff @(posedge clk_a or negedge sync_rst_n)
        if (!sync_rst_n) a_ok <= 1'b0;
        else             a_ok <= d_a;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) a_raw0 <= 1'b0;
        else        a_raw0 <= a_ok;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) a_raw1 <= 1'b0;
        else        a_raw1 <= a_raw0;
    assign q_a = a_raw1;

    // clk_b: two flops on the raw port, no synchroniser in clk_b.
    logic b_raw0, b_raw1;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) b_raw0 <= 1'b0;
        else        b_raw0 <= d_b;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) b_raw1 <= 1'b0;
        else        b_raw1 <= b_raw0;
    assign q_b = b_raw1;

endmodule
