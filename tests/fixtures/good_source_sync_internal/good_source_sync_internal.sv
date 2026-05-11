// Positive fixture: source-synchronous chain wired *internally*.
//
//     A ──► B0 ──► C0
//      └──► B1 ──► C1
//
// Same topology as ``good_source_sync_chain`` but the forwarded clocks
// are not exposed as top-level ports. The only top-level clock input
// is ``ck_a``; A's clock-buffer outputs drive B0/B1's clock inputs
// directly through internal wires, and B0/B1's outputs drive C0/C1.
//
// The CDC contract here is that the system SDC declares each forwarded
// clock with ``create_generated_clock`` on the internal pin where it
// originates (e.g. ``[get_pins u_a/clk_out_b0]`` declares ``ck_b0``).
// The analyzer must stop tracing each flop's clock root at that pin
// and adopt the generated clock's identity, so that ck_b0 resolves to
// ck_a's master domain but is still distinct from ck_a as a clock
// name. Zero async crossings, zero violations.

module block_a_int (
    input  logic clk_in,
    output logic clk_out_b0,
    output logic clk_out_b1,
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

    // Gate-level $_BUF_ primitive per fan-out: prevents the frontend
    // from algebraically aliasing clk_out_b0/b1 back to clk_in (which
    // would make every internal-pin SDC target hit the same bit, and
    // ck_b0/b1 would be indistinguishable from ck_a after the trace).
    // The CDC trace already treats $_BUF_ as transparent on the
    // clock network.
    (* keep *) wire clk_out_b0_w, clk_out_b1_w;
    $_BUF_ u_buf_b0 (.A(clk_in), .Y(clk_out_b0_w));
    $_BUF_ u_buf_b1 (.A(clk_in), .Y(clk_out_b1_w));
    assign clk_out_b0 = clk_out_b0_w;
    assign clk_out_b1 = clk_out_b1_w;
endmodule

module block_b_int (
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

    (* keep *) wire clk_out_w;
    $_BUF_ u_buf (.A(clk_in), .Y(clk_out_w));
    assign clk_out = clk_out_w;
endmodule

module block_c_int (
    input  logic clk_in,
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
endmodule

module good_source_sync_internal (
    input  logic ck_a,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out_c0,
    output logic q_out_c1
);

    logic a_q, b0_q, b1_q, c0_q, c1_q;
    // Internal forwarded clocks: A drives B0/B1, B0 drives C0, B1 drives C1.
    (* keep *) wire ck_b0_int, ck_b1_int, ck_c0_int, ck_c1_int;

    block_a_int u_a  (.clk_in(ck_a),
                      .clk_out_b0(ck_b0_int), .clk_out_b1(ck_b1_int),
                      .rst_n(rst_n), .d_in(d_in), .a_q(a_q));
    block_b_int u_b0 (.clk_in(ck_b0_int), .clk_out(ck_c0_int),
                      .rst_n(rst_n), .d_in(a_q),  .b_q(b0_q));
    block_b_int u_b1 (.clk_in(ck_b1_int), .clk_out(ck_c1_int),
                      .rst_n(rst_n), .d_in(a_q),  .b_q(b1_q));
    block_c_int u_c0 (.clk_in(ck_c0_int),
                      .rst_n(rst_n), .d_in(b0_q), .c_q(c0_q));
    block_c_int u_c1 (.clk_in(ck_c1_int),
                      .rst_n(rst_n), .d_in(b1_q), .c_q(c1_q));

    assign q_out_c0 = c0_q;
    assign q_out_c1 = c1_q;

endmodule
