// Standalone block whose top exercises two yosys-slang strict-default
// rejections that a *block-level* CDC lint must tolerate:
//
//   1. an unconnected top-level SystemVerilog interface port
//      (`bus_if.sink s`) — normal when a block is linted on its own,
//      with no parent to bind the interface;
//   2. a module-item reference to a net declared later in the same
//      module (`sync_q <= meta;` with `meta` declared below) — valid
//      RTL the built-in read_verilog frontend already accepts.
//
// read_slang rejects both by default; rtl-buddy-cdc invokes it with
// --allow-toplevel-iface-ports / --allow-use-before-declare so the
// read_slang frontend stays as lenient as read_verilog. The design
// also carries a real clk_a -> clk_b 8-bit bus crossing (through a 2FF
// sync) so a green elaboration yields an analysable netlist (CDC-004).

interface bus_if (input logic clk);
  logic [7:0] data;
  modport sink (input clk, data);
endinterface

module slang_iface_port_top (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       rst_n,
    input  logic [7:0] d_in,    // clk_a-domain payload
    bus_if.sink        s,       // unconnected top-level interface port
    output logic [7:0] q
);

    // Use-before-declaration: `meta` is referenced here but declared
    // further down. Legal module-item ordering; rejected by read_slang
    // unless --allow-use-before-declare is passed.
    logic [7:0] sync_q;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) sync_q <= '0;
        else        sync_q <= meta;

    logic [7:0] meta;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) meta <= '0;
        else        meta <= src_q;

    logic [7:0] src_q;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) src_q <= '0;
        else        src_q <= d_in ^ s.data;   // consumes the interface payload

    assign q = sync_q;

endmodule
