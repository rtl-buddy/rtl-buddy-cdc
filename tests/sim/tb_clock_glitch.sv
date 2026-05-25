// Testbench for the clock-glitch DUTs.
//
// Drives two ck0-domain clocks (ck0_a, ck0_b) at the same period
// but 180° out of phase, plus a foreign ck1 of incommensurate
// period. The TB toggles `sel_d` at ck1 edges so sel transitions
// land at arbitrary phases of the ck0 inputs — exactly the regime
// that produces runt pulses on the mux output.
//
// The "expected" baseline is the edge count the muxed clock would
// produce if sel never changed (i.e. the count of either ck0_a or
// ck0_b's posedges over the run). Glitchy mux → over-count.
//
// Emits a single `SIM_RESULT errors=N total=M` line:
// - errors = excess_edges = observed_count - expected_baseline
// - total  = expected_baseline
//
// The Python runner parses this line; tests assert errors > 0 on
// the bad DUT and errors == 0 on the safe DUT.

`timescale 1ns/1ps

`ifndef DUT_MODULE
`define DUT_MODULE dut_clock_mux_glitch
`endif

`ifndef RUN_CYCLES
`define RUN_CYCLES 5000
`endif

module tb;
    logic ck0_a = 0;
    logic ck0_b = 1;   // 180° out of phase with ck0_a
    logic ck1   = 0;
    logic sel_d = 0;
    logic [31:0] edge_count;

    // ck0 family: 10 ns full period (5 ns half), 180° offset.
    always #5.0 ck0_a = ~ck0_a;
    always #5.0 ck0_b = ~ck0_b;

    // ck1: incommensurate period to ck0 — sel transitions land in
    // random ck0 phases.
    always #6.7 ck1 = ~ck1;

    // Toggle sel_d at ~30% of ck1 edges.
    int seed = 32'hC0DE_F00D;
    always_ff @(posedge ck1) begin
        if (($dist_uniform(seed, 0, 9)) < 3) sel_d <= ~sel_d;
    end

    `DUT_MODULE u_dut (
        .ck0_a     (ck0_a),
        .ck0_b     (ck0_b),
        .ck1       (ck1),
        .sel_d     (sel_d),
        .edge_count(edge_count)
    );

    // Baseline: count ck0_a posedges over the same run window.
    int baseline = 0;
    always_ff @(posedge ck0_a) baseline <= baseline + 1;

    initial begin
        #1;
        repeat (`RUN_CYCLES) @(posedge ck0_a);
        // Allow a few extra ck0 edges to settle.
        #50;
        // errors = excess edges on the muxed clock vs. baseline.
        // A clean mux emits exactly one edge per ck0 period; a
        // glitching mux emits more on cycles where sel transitioned
        // mid-period.
        $display("SIM_RESULT errors=%0d total=%0d",
                 (edge_count > baseline) ? (edge_count - baseline) : 0,
                 baseline);
        $finish;
    end
endmodule
