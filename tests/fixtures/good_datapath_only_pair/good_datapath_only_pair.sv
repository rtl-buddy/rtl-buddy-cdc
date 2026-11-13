// Positive fixture for set_max_delay -datapath_only as an async hint.
//
// Two unrelated clock domains, ck_a and ck_b, with a flop→flop wire
// between them. The SDC declares nothing async — no set_clock_groups,
// no set_false_path — only a clock-to-clock
// `set_max_delay -datapath_only` bounding the crossing's datapath.
// That bound exists only where the two clocks are not timed against
// each other, so the analyzer reads it as an intended async crossing
// (rtl-buddy-cdc#325) and checks it. A 2FF synchronizer is in place,
// so no violation fires.

module good_datapath_only_pair (
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
