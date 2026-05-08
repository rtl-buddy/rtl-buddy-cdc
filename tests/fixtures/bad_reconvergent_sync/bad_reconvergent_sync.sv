// Negative-case fixture: reconvergent synchronizers.
//
// A single source-domain flop fans out to two independent 2FF
// synchronizer chains in the destination domain. Each chain
// individually filters metastability, but variable resolution times
// can produce *different* synchronized values for one or two
// destination cycles. The downstream combinational reduction across
// the two chains can therefore observe an illegal combined state
// that never existed at the source. Should trip CDC-005.

module bad_reconvergent_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // Two independent synchronizer chains, both fed from src_q. Each
    // stage is a dedicated 1-bit flop so the Yosys netlist exposes the
    // chain explicitly (rather than collapsing into a 2-bit FF with
    // internal feedback).
    logic sync_a_meta, sync_a_q;
    logic sync_b_meta, sync_b_q;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_meta <= 1'b0;
        else        sync_a_meta <= src_q;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_a_q <= 1'b0;
        else        sync_a_q <= sync_a_meta;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_meta <= 1'b0;
        else        sync_b_meta <= src_q;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_b_q <= 1'b0;
        else        sync_b_q <= sync_b_meta;
    end

    // Recombination: ought to be always 0 if the chains stayed aligned,
    // but isn't guaranteed because of independent metastability
    // resolution.
    assign q_out = sync_a_q & ~sync_b_q;

endmodule
