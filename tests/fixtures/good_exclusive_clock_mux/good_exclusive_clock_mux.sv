// Positive fixture for set_clock_groups -physically_exclusive.
//
// Two clock ports drive separate flops with a flop→flop wire between
// them. ck0 and ck1 are declared async AND physically_exclusive — the
// user is asserting that although the static netlist looks like a
// CDC crossing, only one of the two clocks is active in any power
// state, so the path is unreachable and must not be checked.
//
// Without the exclusive declaration this would fire CDC-001 (no
// synchronizer). With it, the analyzer drops the crossing in
// _filter_async before the rule pack sees it, so 0 violations.

module good_exclusive_clock_mux (
    input  logic ck0,
    input  logic ck1,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    (* cdc_sync *) logic d_in_q;
    always_ff @(posedge ck0 or negedge rst_n) begin
        if (!rst_n) d_in_q <= 1'b0;
        else        d_in_q <= d_in;
    end

    logic src_q;
    always_ff @(posedge ck0 or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in_q;
    end

    logic dst_q;
    always_ff @(posedge ck1 or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
