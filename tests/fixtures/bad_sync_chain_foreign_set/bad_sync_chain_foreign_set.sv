// CDC-015 on every asynchronous control pin (rtl-buddy-cdc#390).
//
// Two 2FF synchronisers in dst_clk, each released by a src_clk register
// on a pin other than ARST:
//
//   s1 / s2  $dffsr: CLR from the dst_rst_n port, SET from set_q   CDC-015
//   l1 / l2  $aldff: ALOAD from load_q                             CDC-015
//
// set_q and load_q are src_clk registers, so every release of the set or
// the load races dst_clk. CDC-015 used to read ARST only and missed both
// chains; RDC-001 (which walks every async pin) reports each foreign
// source once. The synchroniser data path is clean: no CDC-001 / -002.
// The committed `.synth.json` (`opt_dff; techmap; abc`) holds the
// gate-level form (`$_DFFSR_*`, `$_ALDFF_*`) and must agree.

module bad_sync_chain_foreign_set (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic dst_rst_n,
    input  logic set_in,
    input  logic load_in,
    input  logic d_in,
    input  logic ad,
    output logic q_s,
    output logic q_l
);
    logic set_q, load_q, src_q;
    always_ff @(posedge src_clk) begin
        set_q  <= set_in;
        load_q <= load_in;
        src_q  <= d_in;
    end

    // BAD: SET from a src_clk register.
    logic s1, s2;
    always_ff @(posedge dst_clk or negedge dst_rst_n or posedge set_q)
        if (!dst_rst_n) s1 <= 1'b0;
        else if (set_q) s1 <= 1'b1;
        else            s1 <= src_q;
    always_ff @(posedge dst_clk or negedge dst_rst_n or posedge set_q)
        if (!dst_rst_n) s2 <= 1'b0;
        else if (set_q) s2 <= 1'b1;
        else            s2 <= s1;
    assign q_s = s2;

    // BAD: ALOAD from a src_clk register.
    logic l1, l2;
    always_ff @(posedge dst_clk or posedge load_q)
        if (load_q) l1 <= ad;
        else        l1 <= src_q;
    always_ff @(posedge dst_clk or posedge load_q)
        if (load_q) l2 <= ad;
        else        l2 <= l1;
    assign q_l = l2;
endmodule
