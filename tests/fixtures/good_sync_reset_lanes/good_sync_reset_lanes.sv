// Synchronous resets that differ by lane, or that one lowering folds
// into SRST in part (rtl-buddy-cdc#398).
//
// `opt_dff` slices a register whose lanes are reset differently into
// one `$sdff` per reset; `proc` leaves one `$dff` with the resets as
// constant-leg muxes on D. The slang frontend folds the first select of
// `if (a) ... else if (b)` into SRST and leaves the second on D. The
// reset-domain map must list the same resets for every form:
//
//   q_split  lanes 1:0 reset by ra, 3:2 by rb      two slices
//   q_part   lanes 1:0 reset by ra, 3:2 never      `$sdff` + `$dff`
//   q_mix    `if (ra) 0101 else if (rb) 0011`      rb folds where the
//                                                  constants agree
//   q_two    `if (ra) ... else if (rb)`            ra | rb, `comb`
//   q_two_n  `if (!ra) ... else if (!rb)`          `comb`, active low
//   q_ce     the same under an enable              `$sdffce`
//   q_rov    ra over an enable, rb under it        ra only
//
// One clock, every input typed to it: the design is clean. The
// `.optdff.json` netlist is the same design after `opt_dff`.

module good_sync_reset_lanes (
    input  logic       clk,
    input  logic       ra,
    input  logic       rb,
    input  logic       en,
    input  logic [3:0] d,
    output logic [3:0] q_split,
    output logic [3:0] q_part,
    output logic [3:0] q_mix,
    output logic [3:0] q_two,
    output logic [3:0] q_two_n,
    output logic [3:0] q_ce,
    output logic [3:0] q_rov
);
    always_ff @(posedge clk) begin
        if (ra) q_split[1:0] <= '0;
        else    q_split[1:0] <= d[1:0];
        if (rb) q_split[3:2] <= '0;
        else    q_split[3:2] <= d[3:2];
    end

    always_ff @(posedge clk) begin
        if (ra) q_part[1:0] <= '0;
        else    q_part[1:0] <= d[1:0];
        q_part[3:2] <= d[3:2];
    end

    always_ff @(posedge clk)
        if (ra)      q_mix <= 4'b0101;
        else if (rb) q_mix <= 4'b0011;
        else         q_mix <= d;

    always_ff @(posedge clk)
        if (ra)      q_two <= '0;
        else if (rb) q_two <= '0;
        else         q_two <= d;

    always_ff @(posedge clk)
        if (!ra)      q_two_n <= '0;
        else if (!rb) q_two_n <= '0;
        else          q_two_n <= d;

    always_ff @(posedge clk)
        if (en) begin
            if (ra)      q_ce <= '0;
            else if (rb) q_ce <= '0;
            else         q_ce <= d;
        end

    always_ff @(posedge clk)
        if (ra) q_rov <= '0;
        else if (en) begin
            if (rb) q_rov <= '0;
            else    q_rov <= d;
        end
endmodule
