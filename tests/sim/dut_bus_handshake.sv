// CDC-004 good DUT (rtl-buddy-cdc#331): a 4-bit payload crosses under
// a four-phase req/ack handshake. The source loads the payload and
// raises `req` together, then holds both until the synchronised ack
// returns; the destination synchronises `req` (2FF), captures the
// payload on its rising edge — a load enable built only from
// dst_clk flops — and returns `ack` through a 2FF into src_clk.
// The payload is stable for at least two destination edges before
// the capture, so no lane can be caught mid-change.
//
// The analyzer accepts the gated capture (CDC-004) and finds the
// synced-back ack that holds the payload (CDC-012): every rule must
// stay silent. The simulation must never show an incoherent capture.

`ifdef SIM_ORACLE_SYNTH
`include "meta_flop_synth.sv"
`else
`include "meta_flop_lib.sv"
`endif

module dut_bus_handshake (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       inc,
    output logic [3:0] q_out
);
    // ---- source domain ------------------------------------------
    // Free-running counter: each transfer sends its current value, so
    // consecutive payloads differ in several bits.
    logic [3:0] cnt = 4'd0;
    always_ff @(posedge src_clk) cnt <= cnt + 4'd1;

    logic ack_meta;
    logic ack_sync = 1'b0;
    logic req      = 1'b0;
    // Payload register; the testbench reads it as `u_dut.src_data`.
    logic [3:0] src_data = 4'd0;
    always_ff @(posedge src_clk) begin
        if (!req && !ack_sync && inc) begin
            req      <= 1'b1;
            src_data <= cnt;
        end else if (req && ack_sync) begin
            req <= 1'b0;
        end
    end

    // ---- destination domain -------------------------------------
    logic req_meta;
    logic req_sync = 1'b0;
    logic ack      = 1'b0;
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0003))
        u_req_meta (.clk(dst_clk), .d(req), .q(req_meta));
    always_ff @(posedge dst_clk) begin
        req_sync <= req_meta;
        ack      <= req_sync;
    end

    // Capture on the rising edge of the synchronised request.
    logic load;
    assign load = req_sync & ~ack;
    logic [3:0] dst_data;
    meta_flop_bus_en #(.WIDTH(4), .RATE_PCT(80), .SEED(32'hB1_0004))
        u_capture (.clk(dst_clk), .en(load), .d(src_data), .q(dst_data));

    // ---- ack back into the source domain ------------------------
    meta_flop_bus #(.WIDTH(1), .RATE_PCT(80), .SEED(32'hB1_0005))
        u_ack_meta (.clk(src_clk), .d(ack), .q(ack_meta));
    always_ff @(posedge src_clk) ack_sync <= ack_meta;

    assign q_out = dst_data;
endmodule
