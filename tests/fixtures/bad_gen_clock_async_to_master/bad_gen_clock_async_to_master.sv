// Negative fixture: a generated clock declared asynchronous to its own
// master (rtl-buddy-cdc#331).
//
// clk drives a divide-by-2 toggle flop declared ck_div2, and ck_div2 a
// second one declared ck_div4 (master ck_div2). The SDC overrides the
// generated relationship for ck_div2 only:
// set_clock_groups -asynchronous -group {clk} -group {ck_div2}. An
// explicit group on the clock names wins over the generated-to-master
// collapse, so:
//   clk → ck_div2    raw single flop → CDC-001
//   ck_div2 → clk    2FF synchroniser, async but clean
// The override covers the named clocks only, as set_clock_groups does
// for STA (a generated clock is not swept into its master's or its
// parent's group unless listed). ck_div4 is unnamed, so it collapses to
// its root clk: the raw clk → ck_div4 and ck_div2 → ck_div4 paths are
// synchronous and silent. Exactly one CDC-001.
//
//   clk ─▶ [div2_q] = ck_div2 (async to clk by SDC) ─▶ [div4_q] = ck_div4
//   [a_q] (clk) ──1FF──▶ [d2_cap] (ck_div2)          → CDC-001
//   [d2_q] (ck_div2) ──2FF──▶ clk                    async, synced
//   [a_q], [d2_q] ──raw──▶ [d4_cap] (ck_div4)        synchronous

module bad_gen_clock_async_to_master (
    input  logic clk,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_2,
    output logic q_div2,
    output logic q_div4,
    output logic q_clk
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

    logic a_q, d2_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge div2_q or negedge rst_n) begin
        if (!rst_n) d2_q <= 1'b0;
        else        d2_q <= d_2;
    end

    // BUG: clk → ck_div2 captured by a single flop, but the SDC says the
    // two are asynchronous.
    logic d2_cap;
    always_ff @(posedge div2_q or negedge rst_n) begin
        if (!rst_n) d2_cap <= 1'b0;
        else        d2_cap <= a_q;
    end
    assign q_div2 = d2_cap;

    // ck_div2 → clk: 2FF synchroniser.
    logic c_s1, c_s2;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            c_s1 <= 1'b0;
            c_s2 <= 1'b0;
        end else begin
            c_s1 <= d2_q;
            c_s2 <= c_s1;
        end
    end
    assign q_clk = c_s2;

    // ck_div4: raw capture from clk and ck_div2, both synchronous.
    logic d4_cap;
    always_ff @(posedge div4_q or negedge rst_n) begin
        if (!rst_n) d4_cap <= 1'b0;
        else        d4_cap <= a_q ^ d2_q;
    end
    assign q_div4 = d4_cap;

endmodule
