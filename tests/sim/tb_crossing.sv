// Generic testbench for a 2-clock single-bit-crossing DUT.
//
// The DUT name is passed as a `+define+DUT_MODULE=<name>` at
// iverilog invocation time (the same TB drives both
// `dut_unsynced` and `dut_2ff`). The TB:
//
//   1. Drives `d_in` from a src_clk-aligned stimulus (a slow toggle
//      every K src-clock cycles, asynchronous to dst_clk).
//   2. Samples `q_out` on dst_clk and compares against a "golden"
//      version of d_in stretched by a fixed latency (1 cycle for
//      the unsynced DUT, 3 cycles for the 2FF DUT).
//   3. Counts the number of dst-clk cycles where the observed
//      q_out disagrees with the golden trace by more than the
//      tolerance window.
//
// Exits with `$display("SIM_RESULT errors=N total=M")` so the Python
// runner can parse a single line.

`timescale 1ns/1ps

`ifndef DUT_MODULE
`define DUT_MODULE dut_unsynced
`endif

`ifndef RUN_CYCLES
`define RUN_CYCLES 5000
`endif

`ifndef LATENCY_CYCLES
`define LATENCY_CYCLES 1
`endif

module tb;
    logic src_clk = 0;
    logic dst_clk = 0;
    logic d_in    = 0;
    logic q_out;

    // Two asynchronous clocks with a deliberately irrational ratio
    // so transitions on src_clk fall in every phase of dst_clk over
    // the run. 10ns vs 7.5ns half-periods (20ns / 15ns full period).
    always #10.0  src_clk = ~src_clk;
    always #7.5   dst_clk = ~dst_clk;

    // Source-side stimulus: toggle d_in on a fraction of src_clk
    // edges. The toggle pattern is deterministic but pseudo-random
    // — see the `$random` calls — so the test exercises many
    // src→dst phase relationships.
    int seed = 32'hDEAD_BEEF;
    always_ff @(posedge src_clk) begin
        if (($dist_uniform(seed, 0, 9)) < 2) d_in <= ~d_in;
    end

    // Golden model: track d_in's history sampled on dst_clk and
    // shifted by LATENCY_CYCLES. A perfect DUT produces q_out equal
    // to the (LATENCY-cycle-old) value of d_in. Cross-domain
    // sampling is itself stochastic, so we mark a cycle as "agree"
    // when q_out matches *either* the LATENCY-shifted value or its
    // immediate neighbours (±1 cycle) — accounts for the
    // ambiguity at the moment of a real transition.
    logic [31:0] history = '0;
    always_ff @(posedge dst_clk) history <= {history[30:0], d_in};

    `DUT_MODULE u_dut (
        .src_clk(src_clk),
        .dst_clk(dst_clk),
        .d_in   (d_in),
        .q_out  (q_out)
    );

    int errors = 0;
    int total  = 0;
    int cycle  = 0;
    logic golden_now;
    logic golden_prev;
    logic golden_next;
    always_ff @(posedge dst_clk) begin
        cycle <= cycle + 1;
        // Skip the first few cycles for the pipeline to fill.
        if (cycle > `LATENCY_CYCLES + 2) begin
            golden_now  = history[`LATENCY_CYCLES];
            golden_prev = history[`LATENCY_CYCLES - 1 >= 0 ?
                                  `LATENCY_CYCLES - 1 : 0];
            golden_next = history[`LATENCY_CYCLES + 1];
            total <= total + 1;
            if (q_out !== golden_now &&
                q_out !== golden_prev &&
                q_out !== golden_next) begin
                errors <= errors + 1;
            end
        end
    end

    initial begin
        #1;
        repeat (`RUN_CYCLES) @(posedge dst_clk);
        $display("SIM_RESULT errors=%0d total=%0d", errors, total);
        $finish;
    end
endmodule
