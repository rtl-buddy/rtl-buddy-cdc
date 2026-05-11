// Negative fixture: same source-synchronous topology as
// good_source_sync_chain (block_a / block_b / block_c with clk_in
// + clk_out clock-buffer ports), but paired with an SDC that fails
// to declare the per-link clocks as related at the system level.
//
//     A ──► B0 ──► C0
//      └──► B1 ──► C1
//
// All four links are direct flop-to-flop captures (correct for
// source-sync timing — no synchronizer expected). When the system
// SDC declares every clock independent and groups them all
// asynchronous — the integration mistake of concatenating block-
// level SDCs without reconciliation — the analyzer correctly
// reports CDC-001 on each of the four links. The fix is the SDC
// shape used by ``good_source_sync_chain``.

module block_a_bad (
    input  logic clk_in,
    output logic clk_out,
    input  logic rst_n,
    input  logic d_in,
    output logic a_q
);
    logic q;
    always_ff @(posedge clk_in or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= d_in;
    end
    assign a_q = q;

    (* keep *) wire clk_buf;
    assign clk_buf = clk_in;
    assign clk_out = clk_buf;
endmodule

module block_b_bad (
    input  logic clk_in,
    output logic clk_out,
    input  logic rst_n,
    input  logic d_in,
    output logic b_q
);
    logic q;
    always_ff @(posedge clk_in or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= d_in;
    end
    assign b_q = q;

    (* keep *) wire clk_buf;
    assign clk_buf = clk_in;
    assign clk_out = clk_buf;
endmodule

module block_c_bad (
    input  logic clk_in,
    output logic clk_out,
    input  logic rst_n,
    input  logic d_in,
    output logic c_q
);
    logic q;
    always_ff @(posedge clk_in or negedge rst_n) begin
        if (!rst_n) q <= 1'b0;
        else        q <= d_in;
    end
    assign c_q = q;

    (* keep *) wire clk_buf;
    assign clk_buf = clk_in;
    assign clk_out = clk_buf;
endmodule

module bad_source_sync_chain (
    input  logic ck_a,
    input  logic ck_b0,
    input  logic ck_b1,
    input  logic ck_c0,
    input  logic ck_c1,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out_c0,
    output logic q_out_c1,
    output logic ck_a_fwd,
    output logic ck_b0_fwd,
    output logic ck_b1_fwd
);

    logic a_q, b0_q, b1_q, c0_q, c1_q;
    logic ck_c0_fwd_unused, ck_c1_fwd_unused;

    block_a_bad u_a  (.clk_in(ck_a),  .clk_out(ck_a_fwd),
                      .rst_n(rst_n), .d_in(d_in), .a_q(a_q));
    block_b_bad u_b0 (.clk_in(ck_b0), .clk_out(ck_b0_fwd),
                      .rst_n(rst_n), .d_in(a_q),  .b_q(b0_q));
    block_b_bad u_b1 (.clk_in(ck_b1), .clk_out(ck_b1_fwd),
                      .rst_n(rst_n), .d_in(a_q),  .b_q(b1_q));
    block_c_bad u_c0 (.clk_in(ck_c0), .clk_out(ck_c0_fwd_unused),
                      .rst_n(rst_n), .d_in(b0_q), .c_q(c0_q));
    block_c_bad u_c1 (.clk_in(ck_c1), .clk_out(ck_c1_fwd_unused),
                      .rst_n(rst_n), .d_in(b1_q), .c_q(c1_q));

    assign q_out_c0 = c0_q;
    assign q_out_c1 = c1_q;

endmodule
