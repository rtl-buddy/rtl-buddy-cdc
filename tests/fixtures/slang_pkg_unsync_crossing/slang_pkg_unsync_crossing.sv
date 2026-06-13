// Negative-case fixture exercised through the yosys-slang plugin.
//
// A `word_t` bus (typedef'd in the imported `cdc_pkg`) crosses from
// src_clk to dst_clk with no gating or gray-coding, so the crossing
// must trip CDC-004 (unprotected bus crossing).
//
// The CDC bug itself is ordinary; what is special is the front door:
// the design pulls its bus type from a separately-compiled package via
// `import cdc_pkg::*`. Yosys's built-in `read_verilog -sv` does not
// elaborate that (it rejects the `word_t` port type), so this fixture
// only loads through `read_slang` (the yosys-slang plugin). It is the
// end-to-end proof that the `--yosys-plugin` / RTL_BUDDY_SLANG_PLUGIN
// path produces a netlist the rule pack can analyse.
//
//        src_clk             dst_clk
//   ┌──[ src_q (word_t) ]──[ dst_q ]──── 8-bit, ungated → CDC-004

import cdc_pkg::*;

module slang_pkg_unsync_crossing (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  word_t       d_in,
    output word_t       q_out
);

    word_t src_q;
    word_t dst_q;

    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= '0;
        else        src_q <= d_in;
    end

    // BAD: single flop on the destination side instead of a 2FF sync.
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) dst_q <= '0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
