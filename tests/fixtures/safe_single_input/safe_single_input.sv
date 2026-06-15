// Safe single-input blackbox fixture (rtl-buddy-cdc#259 audit, FIX 3
// guard against over-firing).
//
// `oneport` is a SINGLE-CLOCK (clk_d) block with exactly ONE
// foreign-domain (clk_x) input crossing entering ONE input port,
// `d_in`. With only one incoming boundary port the reconvergence gate
// (FIX 3) must NOT fire — there is nothing to reconverge at the
// boundary — so the block is still abstracted cleanly and the crossing
// is reported with parity to the flattened design.
//
// FLAT: reports the clk_x -> clk_d crossing.
// BLACKBOXED: abstracted cleanly, the SAME crossing reported (parity),
// and NO decline / reconvergence warning.

module oneport (
    input  wire       clk,
    input  wire [3:0] d_in,
    output wire [3:0] d_out
);
    reg [3:0] s0;
    reg [3:0] s1;
    always_ff @(posedge clk) s0 <= d_in;
    always_ff @(posedge clk) s1 <= s0;
    assign d_out = s1;
endmodule

module top (
    input  wire       clk_x,
    input  wire       clk_d,
    input  wire [3:0] din,
    output wire [3:0] dout
);
    reg [3:0] src_q;
    reg [3:0] dst_q;

    // clk_x source feeding the single foreign-domain input.
    always_ff @(posedge clk_x) src_q <= din;

    wire [3:0] op_out;
    oneport u_oneport (
        .clk  (clk_d),
        .d_in (src_q),
        .d_out(op_out)
    );

    always_ff @(posedge clk_d) dst_q <= op_out;
    assign dout = dst_q;
endmodule
