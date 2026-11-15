// Crossings behind more than four combinational cells
// (rtl-buddy-cdc#347).
//
// Two unsynchronised clk_a -> clk_b paths, each through a six-deep
// AND/OR chain qualified by clk_b-domain inputs: one from the clk_a
// input port `req_a`, one inside `deep_ip` from its clk_a register
// `flag_a`. The crossing walk used to stop after four cells, so neither
// was reported, with no diagnostic; it now walks the whole cone and
// CDC-001 fires on both destinations. `--crossing-depth 4` reproduces
// the old cut and reports it in `summary.crossing_depth_cut`.
//
// `bad_deep_comb_crossing.grey.json` keeps `deep_ip` as a boundary
// cell with its body, so its internal path goes through the
// compositional pass (rtl-buddy-cdc#261) and a cut there is lifted.

module deep_ip (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       set_a,
    input  logic [5:0] q_b,
    output logic       f_out
);
    logic flag_a;
    always_ff @(posedge clk_a) flag_a <= set_a;
    always_ff @(posedge clk_b)
        f_out <= (((((flag_a & q_b[0]) | q_b[1]) & q_b[2]) | q_b[3]) & q_b[4]) | q_b[5];
endmodule

module bad_deep_comb_crossing (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       req_a,
    input  logic       set_a,
    input  logic [5:0] q_b,
    output logic       p_out,
    output logic       f_out
);

    always_ff @(posedge clk_b)
        p_out <= (((((req_a & q_b[0]) | q_b[1]) & q_b[2]) | q_b[3]) & q_b[4]) | q_b[5];

    deep_ip u_ip (
        .clk_a(clk_a), .clk_b(clk_b), .set_a(set_a), .q_b(q_b), .f_out(f_out)
    );

endmodule
