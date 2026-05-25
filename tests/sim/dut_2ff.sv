// Good DUT: 2FF synchroniser. The first dst-domain stage is a
// `meta_flop` (it samples the foreign-domain src signal), the
// second stage is a `safe_flop` (its input is dst-synchronous).
//
// Even with 80% injection on the first stage, the second stage
// resolves the metastability before the value leaves the chain.
// The sim oracle should see q_out track src_q with a fixed latency
// and no extra glitches once the dust settles.

`include "meta_flop_lib.sv"

module dut_2ff (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    safe_flop #(.SEED(32'hA5A5_0001))
        u_src (.clk(src_clk), .d(d_in), .q(src_q));

    logic sync_meta;
    meta_flop #(.RATE_PCT(80), .SEED(32'h1234_5678))
        u_meta (.clk(dst_clk), .d(src_q), .q(sync_meta));

    safe_flop #(.SEED(32'h9876_5432))
        u_sync (.clk(dst_clk), .d(sync_meta), .q(q_out));
endmodule
