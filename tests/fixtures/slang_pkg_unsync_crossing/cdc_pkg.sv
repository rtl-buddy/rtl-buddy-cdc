// Package consumed by slang_pkg_unsync_crossing.sv via `import cdc_pkg::*`.
//
// The package import is the SV-2017 construct that makes this fixture a
// yosys-slang (`read_slang`) exercise: Yosys's built-in `read_verilog -sv`
// frontend does not elaborate a separately-compiled package referenced by
// a module header, so this design only loads through the slang plugin.
package cdc_pkg;
    // Bus width carried across the asynchronous clock-domain boundary.
    parameter int BUS_W = 8;

    typedef logic [BUS_W-1:0] word_t;
endpackage
