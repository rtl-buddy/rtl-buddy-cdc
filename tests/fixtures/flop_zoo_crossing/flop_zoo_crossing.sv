// Flop-type coverage fixture (rtl-buddy-cdc#331).
//
// Five dst_clk flops, one per Yosys flop family the rest of the corpus
// never produces, each capturing the src_clk flop `src_q` directly with
// no synchroniser. The committed netlist is built with `opt_dff` after
// `proc`, which folds the enable and sync-reset muxes into the flops:
//
//   q_aldff   $aldff   async load of `ad`
//   q_dffsr   $dffsr   async set and async clear
//   q_sdffce  $sdffce  sync reset that only acts while `en` is high
//   q_adffe   $adffe   async reset plus enable
//   q_dffe    $dffe    enable
//
// Every capture is a single-flop crossing, so CDC-001 fires five
// times, and nothing else: `q_dffsr`'s CLR is `rst_n ? 0 : 1`, an
// inverter of one reset, not a reset mux (rtl-buddy-cdc#354). The gate-level
// corpus (tests/test_gate_level_corpus.py) lowers the same source to
// `$_ALDFF_*`, `$_DFFSR_*`, `$_SDFFCE_*`, `$_DFFE_*` cells.

module flop_zoo_crossing (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic set_n,
    input  logic aload,
    input  logic ad,
    input  logic en,
    input  logic srst,
    input  logic d_in,
    output logic q_aldff,
    output logic q_dffsr,
    output logic q_sdffce,
    output logic q_adffe,
    output logic q_dffe
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    always_ff @(posedge dst_clk or posedge aload)
        if (aload) q_aldff <= ad;
        else       q_aldff <= src_q;

    always_ff @(posedge dst_clk or negedge rst_n or negedge set_n)
        if (!rst_n)      q_dffsr <= 1'b0;
        else if (!set_n) q_dffsr <= 1'b1;
        else             q_dffsr <= src_q;

    always_ff @(posedge dst_clk)
        if (en) begin
            if (srst) q_sdffce <= 1'b0;
            else      q_sdffce <= src_q;
        end

    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n)  q_adffe <= 1'b0;
        else if (en) q_adffe <= src_q;

    always_ff @(posedge dst_clk)
        if (en) q_dffe <= src_q;
endmodule
