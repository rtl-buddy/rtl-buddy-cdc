// Positive fixture for set_false_path -from -to as an async hint.
//
// Two unrelated clock domains, ck_a and ck_b, with a flop→flop wire
// between them. The SDC declares the pair as a false-path (no
// set_clock_groups -asynchronous needed) — the analyzer treats this
// as equivalent to declaring them async and the rule pack runs as
// usual. With a proper 2FF synchronizer in place, no violation
// fires.

module good_false_path_pair (
    input  logic ck_a,
    input  logic ck_b,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    (* cdc_sync *) logic d_in_q;
    always_ff @(posedge ck_a or negedge rst_n) begin
        if (!rst_n) d_in_q <= 1'b0;
        else        d_in_q <= d_in;
    end

    logic src_q;
    always_ff @(posedge ck_a or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in_q;
    end

    // 2FF synchronizer in ck_b.
    (* cdc_sync *) logic sync_meta;
    always_ff @(posedge ck_b or negedge rst_n) begin
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_q;
    end

    logic sync_q;
    always_ff @(posedge ck_b or negedge rst_n) begin
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;
    end

    assign q_out = sync_q;

endmodule
