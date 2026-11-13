// Negative fixture: virtual clocks typing top-level inputs and outputs,
// with an unsynchronised input and an output launched asynchronously
// to its contract (rtl-buddy-cdc#331).
//
// Same clocks as good_virtual_clock_io: vclk_ext is a virtual clock
// asynchronous to clk, vclk_sys a virtual clock related to it.
//   ext_req  set_input_delay -clock vclk_ext, captured by ONE clk flop:
//            a port-sourced async crossing with no second stage → CDC-001
//   ext_ack  set_output_delay -clock vclk_ext, launched by a clk flop:
//            the external receiver samples it on a clock asynchronous to
//            the launch → CDC-024
//   status   set_output_delay -clock vclk_sys, a gate of two clk flops
//            (req_s1, cfg_q): related clocks, silent
// Every output has a contract, so CDC-025 stays silent. Exactly one
// CDC-001 and one CDC-024.
//
//   ext_req (vclk_ext) ──1FF──▶ [req_s1] clk ──&──▶ status   → CDC-001
//   [cfg_q] clk ──▶ [ack_q] clk ──▶ ext_ack (vclk_ext)       → CDC-024

module bad_virtual_clock_io (
    input  logic clk,
    input  logic rst_n,
    input  logic ext_req,
    input  logic cfg_in,
    output logic status,
    output logic ext_ack
);

    // BUG: ext_req is asynchronous to clk and has one capture flop.
    logic req_s1;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) req_s1 <= 1'b0;
        else        req_s1 <= ext_req;
    end

    // cfg_in: synchronous to clk, a plain register.
    logic cfg_q, ack_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cfg_q <= 1'b0;
            ack_q <= 1'b0;
        end else begin
            cfg_q <= cfg_in;
            ack_q <= cfg_q;
        end
    end

    assign status  = req_s1 & cfg_q;
    // BUG: launched in clk, captured outside on vclk_ext.
    assign ext_ack = ack_q;

endmodule
