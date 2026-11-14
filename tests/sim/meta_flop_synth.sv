// Synthesisable stand-ins for the `meta_flop_lib.sv` modules
// (rtl-buddy-cdc#331).
//
// The simulation library models metastability with `$dist_uniform`,
// which a synthesis frontend cannot read. The analyzer side of the
// oracle needs a netlist of the *same* DUT source, so the DUTs pick
// a library by macro:
//
//     `ifdef SIM_ORACLE_SYNTH  `include "meta_flop_synth.sv"
//     `else                    `include "meta_flop_lib.sv"
//
// and `runner.build_netlist` reads them with `-DSIM_ORACLE_SYNTH`.
// Every module here is the plain flop its simulation twin behaves
// like when no injection fires: same name, ports and parameters, so
// after `flatten` the analyzer sees ordinary `$dff` cells where the
// simulator saw injecting ones.

`ifndef META_FLOP_SYNTH_SV
`define META_FLOP_SYNTH_SV

module meta_flop #(
    parameter RATE_PCT = 30,
    parameter SEED = 32'hC0FFEE01
) (
    input  logic clk,
    input  logic d,
    output logic q
);
    always_ff @(posedge clk) q <= d;
endmodule


module safe_flop #(
    parameter SEED = 32'h0
) (
    input  logic clk,
    input  logic d,
    output logic q
);
    always_ff @(posedge clk) q <= d;
endmodule


module meta_flop_bus #(
    parameter WIDTH = 2,
    parameter RATE_PCT = 80,
    parameter SEED = 32'hC0FFEE02
) (
    input  logic             clk,
    input  logic [WIDTH-1:0] d,
    output logic [WIDTH-1:0] q
);
    always_ff @(posedge clk) q <= d;
endmodule


module meta_flop_bus_en #(
    parameter WIDTH = 2,
    parameter RATE_PCT = 80,
    parameter SEED = 32'hC0FFEE03
) (
    input  logic             clk,
    input  logic             en,
    input  logic [WIDTH-1:0] d,
    output logic [WIDTH-1:0] q
);
    always_ff @(posedge clk) if (en) q <= d;
endmodule

`endif
