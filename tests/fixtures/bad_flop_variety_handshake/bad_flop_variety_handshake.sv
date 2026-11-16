// Flop-type coverage on a broken handshake (rtl-buddy-cdc#331).
//
// good_flop_variety_handshake with four bugs, each on one of its flop
// families (netlist built with `opt_dff` after `proc`):
//
//   ack_sync  $aldff   one-stage ack synchroniser          CDC-001
//   cap_b     $aldffe  loads on the unsynchronised req     CDC-004 x2
//   cap_c     $sdffce  sync reset from a src_clk register  RDC-003, CDC-012
//   cap_a     $dffsre  async clear from a src_clk register RDC-001
//
// cap_b's two CDC-004s are the payload bus and the 1-bit request, which
// fans out to all eight enable lanes. cap_c's reset register is loaded
// under the synchronised enable, but nothing holds it stable: CDC-012
// reports from the data side what RDC-003 reports from the reset side.
// req_s1 / req_s2 (`$dffsr`) are still a correct request synchroniser.
//
// Known gap, pinned as a strict xfail: RDC-001 reads only `$adff*` ARST,
// so cap_a's foreign async clear is not reported as a reset crossing.
// RDC-004 fires instead, reading the constant-leg mux proc builds on CLR
// (`clr_q ? 1 : 0`, a buffer) as glitch-prone logic. The gate-level
// verdict differs too (see the gate-level allowlist); the slang frontend
// agrees with this netlist (rtl-buddy-cdc#384).

module bad_flop_variety_handshake (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       src_rst_n,
    input  logic       src_aload,
    input  logic       src_ad,
    input  logic       src_req,
    input  logic       src_clr,
    input  logic       src_srst,
    input  logic [7:0] src_a,
    input  logic [7:0] src_b,
    input  logic [7:0] src_c,
    input  logic       rst_n,
    input  logic       set_n,
    input  logic       aload,
    input  logic [7:0] ad,
    output logic [7:0] cap_a,
    output logic [7:0] cap_b,
    output logic [7:0] cap_c
);
    // --- src_clk ---
    logic dst_ack;
    logic ack_sync;
    // BAD: one-stage acknowledge synchroniser.
    always_ff @(posedge src_clk or posedge src_aload)
        if (src_aload) ack_sync <= src_ad;
        else           ack_sync <= dst_ack;

    logic src_load_q, clr_q, srst_q;
    always_ff @(posedge src_clk or negedge src_rst_n)
        if (!src_rst_n) begin
            src_load_q <= 1'b0;
            clr_q      <= 1'b1;
            srst_q     <= 1'b0;
        end else begin
            if (src_req)       src_load_q <= 1'b1;
            else if (ack_sync) src_load_q <= 1'b0;
            clr_q  <= src_clr;
            srst_q <= src_srst;
        end

    logic [7:0] pay_a, pay_b, pay_c;
    always_ff @(posedge src_clk or negedge src_rst_n)
        if (!src_rst_n) begin
            pay_a <= 8'h00;
            pay_b <= 8'h00;
            pay_c <= 8'h00;
        end else if (src_req && !src_load_q && !ack_sync) begin
            pay_a <= src_a;
            pay_b <= src_b;
            pay_c <= src_c;
        end

    // --- dst_clk ---
    logic req_s1, req_s2;
    always_ff @(posedge dst_clk or negedge rst_n or negedge set_n)
        if (!rst_n) begin
            req_s1 <= 1'b0;
            req_s2 <= 1'b0;
        end else if (!set_n) begin
            req_s1 <= 1'b1;
            req_s2 <= 1'b1;
        end else begin
            req_s1 <= src_load_q;
            req_s2 <= req_s1;
        end

    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) dst_ack <= 1'b0;
        else        dst_ack <= req_s2;

    // BAD: async clear from a src_clk register.
    always_ff @(posedge dst_clk or posedge clr_q or negedge set_n)
        if (clr_q)        cap_a <= 8'h00;
        else if (!set_n)  cap_a <= 8'hff;
        else if (req_s2)  cap_a <= pay_a;

    // BAD: loaded on the unsynchronised request.
    always_ff @(posedge dst_clk or posedge aload)
        if (aload)           cap_b <= ad;
        else if (src_load_q) cap_b <= pay_b;

    // BAD: sync reset from a src_clk register.
    always_ff @(posedge dst_clk)
        if (req_s2) begin
            if (srst_q) cap_c <= 8'h00;
            else        cap_c <= pay_c;
        end
endmodule
