// A crossing that reaches a handshake capture through its synchronous
// reset (rtl-buddy-cdc#394).
//
// src_clk holds `flag_a` and a clear request `kill_a` until the
// acknowledge, synchronised back, says the transfer is done. dst_clk
// brings the request toggle in through three flops and captures on the
// toggle edge (`s2 ^ s3`), which qualifies the `flag_a` crossing of both
// captures below. `kill_a` is also a src_clk register, used as their
// synchronous reset:
//
//   f_rov  `if (kill_a) clear; else if (edge) load`: the reset wins over
//          the enable (`$sdffe` once folded), so `kill_a` is sampled on
//          every dst_clk edge                               CDC-001
//   f_eor  `if (edge) begin if (kill_a) clear; else load; end`: the
//          reset acts only under the enable (`$sdffce`), so `kill_a` is
//          captured the way `flag_a` is                     no CDC-001
//
// Both foreign sync resets are RDC-003 (one finding, grouped by source).
// The `.optdff.json` netlist is the same design after `opt_dff`; the
// verdict must not change.

module bad_sync_reset_over_enable_capture (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic load,
    input  logic flag_in,
    input  logic kill_in,
    output logic f_rov,
    output logic f_eor
);

    // ---- source side -----------------------------------------------
    logic tog_a = 1'b0;
    logic a1, a2;
    logic flag_a, kill_a;
    wire  idle = (a2 == tog_a);

    always_ff @(posedge src_clk) begin
        a1 <= s3;
        a2 <= a1;
        if (load && idle) begin
            flag_a <= flag_in;
            kill_a <= kill_in;
            tog_a  <= ~tog_a;
        end
    end

    // ---- destination side ------------------------------------------
    logic s1, s2, s3;
    always_ff @(posedge dst_clk) begin
        s1 <= tog_a;
        s2 <= s1;
        s3 <= s2;
        if (kill_a)        f_rov <= 1'b0;
        else if (s2 ^ s3)  f_rov <= flag_a;
        if (s2 ^ s3) begin
            if (kill_a)    f_eor <= 1'b0;
            else           f_eor <= flag_a;
        end
    end

endmodule
