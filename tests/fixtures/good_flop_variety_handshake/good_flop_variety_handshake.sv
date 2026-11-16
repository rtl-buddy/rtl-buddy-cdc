// Flop-type coverage on a correct handshake (rtl-buddy-cdc#331).
//
// A req/ack handshake moves three held 8-bit payloads from src_clk to
// dst_clk. The flops on the crossings and their synchronisers are Yosys
// flop families the rest of the corpus barely produces. The committed
// netlist is built with `opt_dff` after `proc`, which folds the enable
// and sync-reset muxes into the flops:
//
//   ack_meta, ack_sync  $aldff   ack synchroniser, async load of src_ad
//   req_s1, req_s2      $dffsr   req synchroniser, async clear and set
//   cap_a               $dffsre  capture, async clear and set, enable
//   cap_b               $aldffe  capture, async load of ad, enable
//   cap_c               $sdffce  capture, sync reset only while loading
//
// Each payload has its own register, held from request until the
// acknowledge returns, and each capture loads on the synchronised
// request, so CDC-004 and CDC-012 accept all three buses. Nothing fires.
//
// Known gap, pinned as a strict xfail: the slang frontend lowers each
// async load as a reset to 0 and the async set as synchronous logic, so
// it reports CDC-014 between req_s1 and req_s2. At gate level, where
// proc gives every lane of the 8-bit `$dffsre` its own SET / CLR net, the
// lanes are regrouped into one register (rtl-buddy-cdc#386).

module good_flop_variety_handshake (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       src_rst_n,
    input  logic       src_aload,
    input  logic       src_ad,
    input  logic       src_req,
    input  logic [7:0] src_a,
    input  logic [7:0] src_b,
    input  logic [7:0] src_c,
    input  logic       rst_n,
    input  logic       set_n,
    input  logic       aload,
    input  logic [7:0] ad,
    input  logic       srst,
    output logic [7:0] cap_a,
    output logic [7:0] cap_b,
    output logic [7:0] cap_c
);
    // --- src_clk: held request and payload, acknowledge synchroniser ---
    logic dst_ack;
    logic ack_meta, ack_sync;
    always_ff @(posedge src_clk or posedge src_aload)
        if (src_aload) begin
            ack_meta <= src_ad;
            ack_sync <= src_ad;
        end else begin
            ack_meta <= dst_ack;
            ack_sync <= ack_meta;
        end

    logic src_load_q;
    always_ff @(posedge src_clk or negedge src_rst_n)
        if (!src_rst_n)    src_load_q <= 1'b0;
        else if (src_req)  src_load_q <= 1'b1;
        else if (ack_sync) src_load_q <= 1'b0;

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

    // --- dst_clk: request synchroniser, captures, acknowledge ---
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

    always_ff @(posedge dst_clk or negedge rst_n or negedge set_n)
        if (!rst_n)       cap_a <= 8'h00;
        else if (!set_n)  cap_a <= 8'hff;
        else if (req_s2)  cap_a <= pay_a;

    always_ff @(posedge dst_clk or posedge aload)
        if (aload)       cap_b <= ad;
        else if (req_s2) cap_b <= pay_b;

    always_ff @(posedge dst_clk)
        if (req_s2) begin
            if (srst) cap_c <= 8'h00;
            else      cap_c <= pay_c;
        end
endmodule
