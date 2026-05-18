// Negative-case fixture for CDC-009: a single-cycle src-domain pulse
// is sampled by a 10x-slower dst clock. The pulse may land entirely
// between two dst rising edges and be lost (data lost without
// metastability ever entering the picture). CDC-009 must fire.
//
// Shape: event_q is the registered version of an external event;
// event_d is its 1-cycle delay; event_strobe = event_q & ~event_d is
// a 1-cycle pulse on event_q's rising edge. The pulse is sampled by
// the dst-clock 2FF sync chain — which is structurally sound for
// metastability (CDC-001/002 stay silent) but doesn't help with
// pulse-width loss. See issue #47.

module bad_pulse_width_fast_to_slow (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic event_in,
    output logic captured
);

    logic event_q, event_d, event_strobe;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            event_q      <= 1'b0;
            event_d      <= 1'b0;
            event_strobe <= 1'b0;
        end else begin
            event_q      <= event_in;
            event_d      <= event_q;
            event_strobe <= event_q & ~event_d;
        end
    end

    logic captured_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            captured_meta <= 1'b0;
            captured      <= 1'b0;
        end else begin
            captured_meta <= event_strobe;
            captured      <= captured_meta;
        end
    end

endmodule
