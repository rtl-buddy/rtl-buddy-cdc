// A module-item reference to a net declared later in the same module
// (`sync_q <= meta;` with `meta` declared below). Valid SystemVerilog
// that the built-in read_verilog frontend accepts, but yosys-slang's
// read_slang rejects by default. rtl-buddy-cdc passes
// --allow-use-before-declare so the read_slang frontend stays as
// lenient as read_verilog.
//
// Carries a clk_a -> clk_b control crossing through a 2FF synchroniser
// so a green elaboration yields an analysable, violation-free netlist.

module slang_use_before_decl (
    input  logic clk_a,
    input  logic clk_b,
    input  logic rst_n,
    input  logic d_in,    // clk_a-domain control
    output logic q
);

    // `meta` is referenced here but declared further down.
    logic sync_q;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= meta;

    logic meta;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) meta <= 1'b0;
        else        meta <= src_q;

    logic src_q;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    assign q = sync_q;

endmodule
