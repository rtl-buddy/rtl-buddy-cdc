// Positive fixture: a source-synchronous datapath topology.
//
//     A ──► B0 ──► C0
//      └──► B1 ──► C1
//
// Each block (A, B0, B1, C0, C1) is its own clock domain. Every
// block exposes the source-synchronous API:
//
//     clk_in  : the block's local capture clock
//     clk_out : the same clock forwarded out alongside the data
//               (modelled here with an internal clock buffer)
//
// In real silicon, an upstream block's ``clk_out`` is routed (board
// trace, chip pad, repeater chain) to the downstream block's
// ``clk_in``. At lint scope we model the forwarded clock as a
// distinct top-level input port per block — that mirrors how SoC
// integration usually presents the system to a CDC tool — and route
// each block's ``clk_out`` to a top-level output port representing
// the forwarded clock leaving the chip.
//
// Links are direct flop-to-flop captures with no synchronizer
// (correct for source-sync timing). The CDC contract is "the system
// SDC must declare each link's two clocks as related". The paired
// SDC uses ``create_generated_clock`` so all five clocks collapse to
// ck_a's master domain — the analyzer sees four raw clock-name
// mismatches but ``are_async`` returns False for each, so zero async
// crossings and zero violations are reported.
//
// The negative counterpart ``bad_source_sync_chain`` uses identical
// RTL but a system SDC that fails to declare the relations, and
// must fire CDC-001 on each of the four links.

// Block A: launching block. Registers the system input on clk_in
// and forwards a buffered copy of clk_in out as clk_out.
module block_a (
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

    // Clock buffer: forwards clk_in out source-synchronously with
    // the data. ``(* keep *)`` prevents the optimizer from collapsing
    // the wire away, so the buffer survives in the post-flatten
    // netlist as documentation of intent.
    (* keep *) wire clk_buf;
    assign clk_buf = clk_in;
    assign clk_out = clk_buf;
endmodule

// Block B (used for both B0 and B1): source-synchronous capture
// from upstream A. The captured value is exposed as ``b_q`` and is
// also the launch register feeding the downstream C block.
module block_b (
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

// Block C (used for both C0 and C1): terminal source-synchronous
// capture from upstream B. ``clk_out`` is forwarded for symmetry —
// in a real system this might drive a downstream chiplet or be
// left as a test/observation pin.
module block_c (
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

module good_source_sync_chain (
    input  logic ck_a,
    input  logic ck_b0,
    input  logic ck_b1,
    input  logic ck_c0,
    input  logic ck_c1,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out_c0,
    output logic q_out_c1,
    // Forwarded source-synchronous clocks leaving the top. Each is
    // the buffered clk_out of the corresponding block — in real
    // silicon these go off-chip alongside the data.
    output logic ck_a_fwd,
    output logic ck_b0_fwd,
    output logic ck_b1_fwd
);

    logic a_q, b0_q, b1_q, c0_q, c1_q;
    logic ck_c0_fwd_unused, ck_c1_fwd_unused;

    block_a u_a  (.clk_in(ck_a),  .clk_out(ck_a_fwd),
                  .rst_n(rst_n), .d_in(d_in), .a_q(a_q));
    block_b u_b0 (.clk_in(ck_b0), .clk_out(ck_b0_fwd),
                  .rst_n(rst_n), .d_in(a_q),  .b_q(b0_q));
    block_b u_b1 (.clk_in(ck_b1), .clk_out(ck_b1_fwd),
                  .rst_n(rst_n), .d_in(a_q),  .b_q(b1_q));
    block_c u_c0 (.clk_in(ck_c0), .clk_out(ck_c0_fwd_unused),
                  .rst_n(rst_n), .d_in(b0_q), .c_q(c0_q));
    block_c u_c1 (.clk_in(ck_c1), .clk_out(ck_c1_fwd_unused),
                  .rst_n(rst_n), .d_in(b1_q), .c_q(c1_q));

    assign q_out_c0 = c0_q;
    assign q_out_c1 = c1_q;

endmodule
