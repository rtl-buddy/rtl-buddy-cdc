// Negative-case fixture: a synchronizer first stage in dst_clk is fed
// by combinational logic of top-level inputs, with no source-domain
// flop registering the value. Even though dst_clk has a 2FF chain to
// filter metastability, the unregistered comb source can glitch
// (during simultaneous transitions of `a` and `b`), and the sync may
// sample a transient value that never represented a stable input
// state. Should trip CDC-006.

module bad_comb_source (
    input  logic dst_clk,
    input  logic rst_n,
    input  logic a,
    input  logic b,
    output logic q_out
);

    logic sync_meta;
    logic sync_q;

    // BAD: comb source fed by a, b directly — no registering flop
    // before the synchronizer.
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= a & b;
    end

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
