// Positive-case fixture for CDC-009: the textbook req/ack handshake
// from issue #47 §5 idiom 3. The src request flop is held high until
// the dst-side ack returns (synced back through a 2FF chain). Because
// the value is held across many src cycles, dst always sees a stable
// signal — no pulse-loss possible. CDC-009 must NOT fire (the src
// flop's D pin is a priority-encoded $mux nest, not an ``A & ~A_d``
// edge-detector).

module good_pulse_width_handshake (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic req_in,
    output logic captured
);

    logic ack_meta, ack_sync;
    logic dst_ack;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            ack_meta <= 1'b0;
            ack_sync <= 1'b0;
        end else begin
            ack_meta <= dst_ack;
            ack_sync <= ack_meta;
        end
    end

    logic req_held;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)         req_held <= 1'b0;
        else if (req_in)    req_held <= 1'b1;
        else if (ack_sync)  req_held <= 1'b0;
    end

    logic req_meta, req_sync;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            req_meta <= 1'b0;
            req_sync <= 1'b0;
            dst_ack  <= 1'b0;
            captured <= 1'b0;
        end else begin
            req_meta <= req_held;
            req_sync <= req_meta;
            dst_ack  <= req_sync;
            captured <= req_sync;
        end
    end

endmodule
