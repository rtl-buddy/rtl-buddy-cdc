// `--ignore-scan-mode` positive case (issue #45) — every async
// crossing in this design exists only because of the DFT scan clock
// mux, so the flag clears the report and its absence leaves it firing.
//
// `dst_q` is clocked through `$mux(S=scan_en, A=func_clk, B=scan_clk)`
// — the textbook scan-mux shape a DFT insertion flow drops in front of
// a flop so the scan chain can shift on the tester's clock. The two
// legs are asynchronous to each other, so the analyzer resolves the
// muxed net to one of them (`func_clk`, the first leg that resolves)
// while the source flop `src_q` sits in `scan_clk`. The result reads
// as an unsynchronized `scan_clk -> func_clk` control crossing and
// CDC-001 fires — even though the two clocks never drive this flop in
// the same mode, and the scan path is not exercised functionally.
//
// The `(* scan_en *)` attribute on the select port is what tells the
// analyzer which pin is the test-mode control. With
// `--ignore-scan-mode` the crossing is skipped and the design reports
// clean; without it, nothing changes and CDC-001 still fires. The
// crossing is always *tagged*, never dropped, and the suppressed count
// is reported.
//
// Build:
//   yosys -p 'read_verilog -sv good_scan_mode_ignored.sv; \
//             hierarchy -top good_scan_mode_ignored; proc; flatten; \
//             write_json good_scan_mode_ignored.json'

module good_scan_mode_ignored (
    input  logic func_clk,
    input  logic scan_clk,
    input  logic rst_n,
    // The DFT test-mode control. The attribute is the whole point:
    // structurally this pin is indistinguishable from a runtime clock
    // select, which WOULD be a genuine hazard.
    (* scan_en *) input logic scan_en,
    input  logic d_in,
    output logic dst_q
);

    // Scan-shift source: a flop in the tester's clock domain.
    logic src_q;
    always_ff @(posedge scan_clk or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    // The DFT clock mux. In functional mode `func_clk` reaches the
    // flop; in scan mode `scan_clk` does. Never both.
    logic muxed_clk;
    assign muxed_clk = scan_en ? scan_clk : func_clk;

    // Destination flop, clocked through the mux — the crossing the
    // flag is about.
    always_ff @(posedge muxed_clk or negedge rst_n)
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;

endmodule
