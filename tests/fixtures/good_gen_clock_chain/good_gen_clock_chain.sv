// Positive fixture: a generated clock of a generated clock
// (rtl-buddy-cdc#331).
//
// clk drives a divide-by-2 toggle flop whose output net div2_q is
// declared ck_div2 (create_generated_clock -divide_by 2 on the pin).
// ck_div2 drives a second toggle flop, div4_q, declared ck_div4 with
// -master_clock ck_div2. Both collapse to the root clk, so the raw
// single-flop paths clk → ck_div4 and ck_div2 → ck_div4 are
// synchronous: STA times them and they are not CDC crossings. clk_ext is
// asynchronous to clk (set_clock_groups -asynchronous names the roots);
// it reaches ck_div4 through the root, so clk_ext → ck_div4 and
// ck_div4 → clk_ext are async crossings, each into a 2FF synchroniser.
// Two async crossings, no finding.
//
//   clk ─▶ [div2_q] = ck_div2 ─▶ [div4_q] = ck_div4
//   [a_q] (clk) ──raw──▶ [d4_cap] ◀──raw── [d2_q] (ck_div2)   synchronous
//   clk_ext ──2FF──▶ ck_div4 ──2FF──▶ clk_ext                 async, synced

module good_gen_clock_chain (
    input  logic clk,
    input  logic clk_ext,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_ext,
    output logic q_div4,
    output logic q_ext
);

    // Clock dividers: the generated-clock source pins.
    logic div2_q, div4_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) div2_q <= 1'b0;
        else        div2_q <= ~div2_q;
    end
    always_ff @(posedge div2_q or negedge rst_n) begin
        if (!rst_n) div4_q <= 1'b0;
        else        div4_q <= ~div4_q;
    end

    logic a_q, d2_q, ext_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge div2_q or negedge rst_n) begin
        if (!rst_n) d2_q <= 1'b0;
        else        d2_q <= a_q;
    end
    always_ff @(posedge clk_ext or negedge rst_n) begin
        if (!rst_n) ext_q <= 1'b0;
        else        ext_q <= d_ext;
    end

    // ck_div4: raw capture from clk and ck_div2 (synchronous) plus a 2FF
    // synchroniser from clk_ext (asynchronous).
    logic d4_cap, d4_s1, d4_s2;
    always_ff @(posedge div4_q or negedge rst_n) begin
        if (!rst_n) begin
            d4_cap <= 1'b0;
            d4_s1  <= 1'b0;
            d4_s2  <= 1'b0;
        end else begin
            d4_cap <= a_q ^ d2_q;
            d4_s1  <= ext_q;
            d4_s2  <= d4_s1;
        end
    end

    // ck_div4 → clk_ext: 2FF synchroniser.
    logic e_s1, e_s2;
    always_ff @(posedge clk_ext or negedge rst_n) begin
        if (!rst_n) begin
            e_s1 <= 1'b0;
            e_s2 <= 1'b0;
        end else begin
            e_s1 <= d4_cap;
            e_s2 <= e_s1;
        end
    end

    assign q_div4 = d4_cap ^ d4_s2;
    assign q_ext  = e_s2;

endmodule
