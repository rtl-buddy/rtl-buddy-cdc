// `--ignore-scan-mode` control case (issue #45) — the flag must not
// become a blanket "stop checking this design".
//
// Same DFT scan clock mux as `good_scan_mode_ignored`
// (`$mux(S=scan_en, A=func_clk, B=scan_clk)` in front of `dst_q`), so
// the scan-path crossing is suppressible. Alongside it sits a second,
// entirely functional crossing that has nothing to do with DFT:
// `other_q` in the `other_clk` domain feeds `func_q`, a flop clocked
// directly by `func_clk` with no mux in its clock network. That flop's
// CLK fanin contains no scan-mode port, so the crossing is never
// tagged and never suppressed.
//
// Expected: CDC-001 fires twice without the flag, and once — on the
// functional path — with it. A tool that reported this design clean
// under `--ignore-scan-mode` would be hiding a real bug behind a DFT
// annotation.
//
// Build:
//   yosys -p 'read_verilog -sv bad_scan_mode_functional_crossing.sv; \
//             hierarchy -top bad_scan_mode_functional_crossing; proc; flatten; \
//             write_json bad_scan_mode_functional_crossing.json'

module bad_scan_mode_functional_crossing (
    input  logic func_clk,
    input  logic scan_clk,
    input  logic other_clk,
    input  logic rst_n,
    (* scan_en *) input logic scan_en,
    input  logic d_in,
    input  logic e_in,
    output logic dst_q,
    output logic func_q
);

    // --- scan path: suppressible ------------------------------------
    logic src_q;
    always_ff @(posedge scan_clk or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    logic muxed_clk;
    assign muxed_clk = scan_en ? scan_clk : func_clk;

    always_ff @(posedge muxed_clk or negedge rst_n)
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;

    // --- functional path: NOT suppressible --------------------------
    // A plain unsynchronized other_clk -> func_clk crossing. No mux in
    // the destination's clock network, so no scan tag.
    logic other_q;
    always_ff @(posedge other_clk or negedge rst_n)
        if (!rst_n) other_q <= 1'b0;
        else        other_q <= e_in;

    always_ff @(posedge func_clk or negedge rst_n)
        if (!rst_n) func_q <= 1'b0;
        else        func_q <= other_q;

endmodule
