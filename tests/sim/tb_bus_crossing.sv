// Testbench for multi-bit crossing DUTs (rtl-buddy-cdc#331): the
// CDC-004 bus DUTs (`dut_bus_*`) and the CDC-005 reconvergence DUTs
// (`dut_reconv_*`).
//
// DUT contract
// ------------
// Ports `src_clk`, `dst_clk`, `inc` (1-bit, src_clk-synchronous
// stimulus) and `q_out[BUS_W-1:0]` (dst_clk-synchronous). The DUT also
// exposes `src_data[BUS_W-1:0]`, the multi-bit value the source
// presents to the crossing, which this testbench reads hierarchically
// as `u_dut.src_data` (no extra port, so the analyzer sees the same
// interface the simulator drives). For a reconvergence DUT `src_data`
// is the pair of values the source hands its two consumers, e.g.
// `{src_q, src_q}` when one bit fans out to two synchronisers.
//
// Scoreboard property ("coherent sample")
// ---------------------------------------
// Let SRC be the set of (value, interval) pairs `src_data` held over
// the run, recorded on every change with `$realtime`. At each dst_clk
// posedge t after warm-up the testbench reads `q_out` (the value the
// DUT registered on an earlier edge) and counts an error iff
//
//     no value in SRC whose holding interval intersects
//     [t - WINDOW_DST_CYCLES * T_dst, t] equals q_out.
//
// In words: the destination may present any value the source really
// held within the last WINDOW_DST_CYCLES destination periods — stale
// by the synchroniser's latency, or by one more cycle when injection
// delays a lane — but never a value the source did not hold. An
// incoherent multi-bit sample (some lanes from the old value, some
// from the new) or a reconvergent pair whose halves came from
// different source instants is exactly such a value. The window must
// cover the DUT's worst-case latency, or a correct DUT would fail;
// widening it can only hide errors (a mix that coincides with a
// recently-held value), never invent them.
//
// Liveness: `updates` counts dst cycles on which `q_out` changed, so a
// test can reject a DUT that passes only because it never moves.
//
// Run-time knobs (plusargs, so one compiled image serves every seed
// and phase relationship):
//   +SRC_HALF_PS=<n>  src_clk half period in ps (default 10000)
//   +DST_HALF_PS=<n>  dst_clk half period in ps (default 7500)
//   +SRC_PHASE_PS=<n> delay before src_clk starts toggling (default 0)
//   +STIM_SEED=<n>    seed of the `inc` stimulus
//   +INC_PCT=<n>      probability (%) that `inc` is high on a src edge
//   +META_SEED=<n>    read by `meta_flop_bus*` to reseed injection
//
// Output: `SIM_RESULT errors=N total=M`, `SIM_STATS updates=U
// src_changes=C`, and up to five `SIM_ERROR` lines describing the
// first failures.

`timescale 1ns/1ps

`ifndef DUT_MODULE
`define DUT_MODULE dut_bus_binary
`endif

`ifndef BUS_W
`define BUS_W 4
`endif

`ifndef RUN_CYCLES
`define RUN_CYCLES 5000
`endif

`ifndef WINDOW_DST_CYCLES
`define WINDOW_DST_CYCLES 6
`endif

module tb;
    localparam int W    = `BUS_W;
    localparam int HIST = 256;

    int src_half_ps  = 10000;
    int dst_half_ps  = 7500;
    int src_phase_ps = 0;
    int inc_pct      = 50;
    int stim_seed    = 32'h5EED_0001;

    logic src_clk = 1'b0;
    logic dst_clk = 1'b0;
    logic inc     = 1'b0;
    logic [W-1:0] q_out;

    initial begin
        if ($value$plusargs("SRC_HALF_PS=%d", src_half_ps)) ;
        if ($value$plusargs("DST_HALF_PS=%d", dst_half_ps)) ;
        if ($value$plusargs("SRC_PHASE_PS=%d", src_phase_ps)) ;
        if ($value$plusargs("STIM_SEED=%d", stim_seed)) ;
        if ($value$plusargs("INC_PCT=%d", inc_pct)) ;
        fork
            begin
                #(src_phase_ps / 1000.0);
                forever #(src_half_ps / 1000.0) src_clk = ~src_clk;
            end
            forever #(dst_half_ps / 1000.0) dst_clk = ~dst_clk;
        join_none
    end

    always_ff @(posedge src_clk)
        inc <= ($dist_uniform(stim_seed, 0, 99) < inc_pct);

    `DUT_MODULE u_dut (
        .src_clk(src_clk),
        .dst_clk(dst_clk),
        .inc    (inc),
        .q_out  (q_out)
    );

    // ---- source history ----------------------------------------
    logic [W-1:0] hist_v [HIST];
    realtime      hist_t [HIST];
    int           hist_n = 0;

    task automatic push(input logic [W-1:0] v);
        hist_v[hist_n % HIST] = v;
        hist_t[hist_n % HIST] = $realtime;
        hist_n = hist_n + 1;
    endtask

    initial begin
        #0;
        push(u_dut.src_data);
    end
    always @(u_dut.src_data) push(u_dut.src_data);

    // True iff `v` equals a value `src_data` held at some instant in
    // [now - win, now]. Walks the history newest-first; entry k was
    // held from hist_t[k] until the next entry's timestamp.
    function automatic bit legal(input logic [W-1:0] v,
                                 input realtime now,
                                 input realtime win);
        realtime held_until;
        for (int k = hist_n - 1; k >= 0 && k > hist_n - HIST; k--) begin
            held_until = (k == hist_n - 1) ? now : hist_t[(k + 1) % HIST];
            if (held_until < now - win) return 1'b0;
            if (hist_v[k % HIST] === v) return 1'b1;
        end
        return 1'b0;
    endfunction

    // ---- scoreboard ----------------------------------------------
    int errors  = 0;
    int total   = 0;
    int updates = 0;
    int cycle   = 0;
    logic [W-1:0] q_prev;
    realtime win;

    always @(posedge dst_clk) begin
        cycle = cycle + 1;
        win   = `WINDOW_DST_CYCLES * 2.0 * dst_half_ps / 1000.0;
        if (cycle > `WINDOW_DST_CYCLES + 2) begin
            total = total + 1;
            if (q_out !== q_prev) updates = updates + 1;
            if (!legal(q_out, $realtime, win)) begin
                if (errors < 5)
                    $display("SIM_ERROR t=%0t q_out=%b src_data=%b",
                             $realtime, q_out, u_dut.src_data);
                errors = errors + 1;
            end
        end
        q_prev = q_out;
    end

    initial begin
        #1;
        repeat (`RUN_CYCLES) @(posedge dst_clk);
        $display("SIM_RESULT errors=%0d total=%0d", errors, total);
        $display("SIM_STATS updates=%0d src_changes=%0d", updates, hist_n - 1);
        $finish;
    end
endmodule
