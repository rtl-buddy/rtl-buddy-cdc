// Positive-case fixture for CDC-009: the textbook pulse-stretcher
// fix from issue #47 §5 idiom 1. A 4-bit countdown counter widens the
// src-domain strobe to 16 src cycles (~32 ns at 500 MHz) — comfortably
// wider than the 20 ns dst period, so dst always captures it. CDC-009
// must NOT fire (the src flop's D pin is ``cnt != 0``, which yosys
// synthesises as a reduction, not the ``A & ~A_d`` edge-detector
// pattern the rule keys on).

module good_pulse_width_stretched (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic event_in,
    output logic captured
);

    logic event_q, event_d, rising_edge;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            event_q <= 1'b0;
            event_d <= 1'b0;
        end else begin
            event_q <= event_in;
            event_d <= event_q;
        end
    end
    assign rising_edge = event_q & ~event_d;

    logic [3:0] cnt;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)             cnt <= 4'd0;
        else if (rising_edge)   cnt <= 4'd15;
        else if (cnt != 4'd0)   cnt <= cnt - 4'd1;
    end

    logic stretched_strobe;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) stretched_strobe <= 1'b0;
        else        stretched_strobe <= (cnt != 4'd0);
    end

    logic strobe_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            strobe_meta <= 1'b0;
            captured    <= 1'b0;
        end else begin
            strobe_meta <= stretched_strobe;
            captured    <= strobe_meta;
        end
    end

endmodule
