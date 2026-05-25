// Testbench for reset-crossing DUTs.
//
// Two asynchronous clocks, a global reset (driven low for the
// startup window then released), and a `local_rst_req` pulse that
// fires periodically on src_clk and triggers a reset deassertion
// edge into the destination domain. The TB samples q_out post-reset
// and counts cycles where q_out disagrees with the golden trace by
// more than the tolerance window.
//
// Compared to `tb_crossing.sv`, this TB:
//   - drives `local_rst_req` (asserted for one src_clk pulse then
//     deasserted, repeated every ~50 src cycles); the resulting
//     `local_rst_n` deassertion edge is the recovery/removal hazard
//     under test;
//   - assumes the DUT exposes `(src_clk, dst_clk, global_rst_n,
//     local_rst_req, d_in, q_out)` (matches dut_rdc001_*);
//   - tracks the post-reset settling window explicitly; errors are
//     counted only outside that window.

`timescale 1ns/1ps

`ifndef DUT_MODULE
`define DUT_MODULE dut_rdc001_bad
`endif

`ifndef RUN_CYCLES
`define RUN_CYCLES 6000
`endif

`ifndef LATENCY_CYCLES
`define LATENCY_CYCLES 1
`endif

module tb;
    logic src_clk        = 0;
    logic dst_clk        = 0;
    logic global_rst_n   = 0;
    logic local_rst_req  = 0;
    logic d_in           = 0;
    logic q_out;

    always #10.0  src_clk = ~src_clk;
    always #7.5   dst_clk = ~dst_clk;

    int seed_d  = 32'hABCDEF01;
    int seed_r  = 32'h13579BDF;

    // Data stimulus
    always_ff @(posedge src_clk) begin
        if (($dist_uniform(seed_d, 0, 9)) < 2) d_in <= ~d_in;
    end

    // Reset stimulus: pulse local_rst_req every ~50 src_clk cycles.
    // The deassertion of local_rst_req (DUT inverts to local_rst_n)
    // is the recovery/removal hazard.
    int rst_cycle = 0;
    always_ff @(posedge src_clk) begin
        rst_cycle <= rst_cycle + 1;
        if (rst_cycle % 50 == 5) local_rst_req <= 1'b1;
        else                     local_rst_req <= 1'b0;
    end

    // Settling window: after each reset-release event, mask errors
    // for N dst_clk cycles. Reset crossing failures show up as
    // X-likely transients in the first few cycles post-release; we
    // want to count those, but not count the long post-reset reset
    // value differing from the golden trace.
    int settle_left = 0;
    logic local_rst_q_dst;
    safe_flop u_track_rst (
        .clk(dst_clk), .d(local_rst_req), .q(local_rst_q_dst)
    );
    logic local_rst_q_dst_prev = 0;
    always_ff @(posedge dst_clk) begin
        local_rst_q_dst_prev <= local_rst_q_dst;
        // Falling edge of local_rst_q_dst → deassertion event
        if (local_rst_q_dst_prev && !local_rst_q_dst) settle_left <= 8;
        else if (settle_left > 0) settle_left <= settle_left - 1;
    end

    logic [31:0] history = 0;
    always_ff @(posedge dst_clk) history <= {history[30:0], d_in};

    `DUT_MODULE u_dut (
        .src_clk      (src_clk),
        .dst_clk      (dst_clk),
        .global_rst_n (global_rst_n),
        .local_rst_req(local_rst_req),
        .d_in         (d_in),
        .q_out        (q_out)
    );

    int errors = 0;
    int total  = 0;
    int cycle  = 0;
    logic g_now, g_prev, g_next;
    always_ff @(posedge dst_clk) begin
        cycle <= cycle + 1;
        if (cycle > `LATENCY_CYCLES + 2 && settle_left == 0) begin
            g_now  = history[`LATENCY_CYCLES];
            g_prev = history[`LATENCY_CYCLES - 1 >= 0 ?
                             `LATENCY_CYCLES - 1 : 0];
            g_next = history[`LATENCY_CYCLES + 1];
            total <= total + 1;
            if (q_out !== g_now && q_out !== g_prev && q_out !== g_next) begin
                errors <= errors + 1;
            end
        end
    end

    initial begin
        #5 global_rst_n = 1;  // release global reset after a few ns
        repeat (`RUN_CYCLES) @(posedge dst_clk);
        $display("SIM_RESULT errors=%0d total=%0d", errors, total);
        $finish;
    end
endmodule
