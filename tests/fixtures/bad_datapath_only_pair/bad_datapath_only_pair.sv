// Negative fixture for set_max_delay -datapath_only as an async hint.
//
// Two unrelated clock domains, ck_a and ck_b, with a raw flop→flop wire
// between them and no synchronizer. The SDC declares nothing async —
// only a clock-to-clock `set_max_delay -datapath_only`. Before
// rtl-buddy-cdc#325 that command was dropped as STA noise, the pair
// looked synchronous and the crossing was never checked; reading the
// datapath-only bound as an intended async crossing makes CDC-001 fire.

module bad_datapath_only_pair (
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

    // Single capture flop in ck_b — no second synchronizer stage.
    logic dst_q;
    always_ff @(posedge ck_b or negedge rst_n) begin
        if (!rst_n) dst_q <= 1'b0;
        else        dst_q <= src_q;
    end

    assign q_out = dst_q;

endmodule
