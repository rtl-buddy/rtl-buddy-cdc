// A 1-bit payload captured under a toggle handshake's synchronised
// enable (rtl-buddy-cdc#368).
//
// src_clk loads an 8-bit `data_a` and a 1-bit `flag_a` (and flips the
// request toggle `tog_a`) only when the acknowledge, synchronised back
// into src_clk, says the previous transfer is done. dst_clk brings the
// toggle in through three flops and captures both registers on the
// toggle edge (`s2 ^ s3`). The flag is protected exactly as the bus
// is: held by the source until the acknowledge returns, sampled only
// on the synchronised edge. CDC-004 accepts the bus as a gated
// crossing; CDC-001 must accept the flag the same way. `flag_r` is a
// second capture of the flag whose register also has a synchronous
// dst_clk reset, so `proc` leaves a reset mux in front of its hold mux.
//
// The `.optdff.json` netlist is the same design after `opt_dff`, which
// folds both captures' hold muxes into `$dffe` enables; the
// `.synth.json` netlist is the gate-level synth leg (`$_DFFE_*`).
//
// Paired with `bad_handshake_single_bit` (free-running, unsynchronised,
// third-domain and open-loop enables, and a data-select mux).

module good_handshake_single_bit (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       rst_b,
    input  logic       load,
    input  logic [7:0] data_in,
    input  logic       flag_in,
    output logic [7:0] data_out,
    output logic       flag_out,
    output logic       flag_r
);

    // ---- source side -----------------------------------------------
    logic       tog_a = 1'b0;
    logic       a1, a2;
    logic [7:0] data_a;
    logic       flag_a;
    wire        idle = (a2 == tog_a);

    always_ff @(posedge src_clk) begin
        a1 <= s3;
        a2 <= a1;
        if (load && idle) begin
            data_a <= data_in;
            flag_a <= flag_in;
            tog_a  <= ~tog_a;
        end
    end

    // ---- destination side ------------------------------------------
    logic s1, s2, s3;
    always_ff @(posedge dst_clk) begin
        s1 <= tog_a;
        s2 <= s1;
        s3 <= s2;
        if (s2 ^ s3) begin
            data_out <= data_a;
            flag_out <= flag_a;
            flag_r   <= flag_a;
        end
        if (rst_b) flag_r <= 1'b0;
    end

endmodule
