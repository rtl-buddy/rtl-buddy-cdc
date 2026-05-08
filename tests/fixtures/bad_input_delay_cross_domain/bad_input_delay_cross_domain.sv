// Negative fixture: set_input_delay declares port `a` as being in
// foreign_clk's domain, and a 2FF synchronizer in dst_clk samples it
// through combinational logic. The SDC explicitly says these clocks
// are async, so the synchronizer is reading a cross-domain comb
// signal — must trip CDC-006 (with the source clock named in the
// message, since both ends are typed).

module bad_input_delay_cross_domain (
    input  logic dst_clk,
    input  logic rst_n,
    input  logic a,
    input  logic b,
    output logic q_out
);

    logic sync_meta;
    logic sync_q;

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
