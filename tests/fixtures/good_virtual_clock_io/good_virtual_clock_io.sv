// Positive fixture: virtual clocks typing top-level inputs and outputs
// (rtl-buddy-cdc#331).
//
// clk is the only clock with a port. Two virtual clocks (create_clock
// with no source object) describe the outside world: vclk_ext, an
// external agent declared asynchronous to clk, and vclk_sys, the board
// clock clk is derived from (no group separates it from clk, so the two
// are related and timed together).
//   ext_req  set_input_delay -clock vclk_ext: a port-sourced async
//            crossing into clk, landing in a 2FF synchroniser
//   cfg_in   set_input_delay -clock vclk_sys: synchronous, a plain register
//   status   set_output_delay -clock vclk_sys, launched in clk: the
//            receiver's clock is related to the launch (CDC-024 silent)
//   ack_out  set_output_delay -clock clk, launched in clk
// Every output has a contract (CDC-025 silent). One async crossing, no
// finding.
//
//   ext_req (vclk_ext) ──2FF──▶ clk     cfg_in (vclk_sys) ──▶ [cfg_q] clk
//   [st_q] clk ──▶ status (vclk_sys)    [ack_q] clk ──▶ ack_out (clk)

module good_virtual_clock_io (
    input  logic clk,
    input  logic rst_n,
    input  logic ext_req,
    input  logic cfg_in,
    output logic status,
    output logic ack_out
);

    // ext_req: asynchronous to clk, 2FF synchroniser.
    logic req_s1, req_s2;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            req_s1 <= 1'b0;
            req_s2 <= 1'b0;
        end else begin
            req_s1 <= ext_req;
            req_s2 <= req_s1;
        end
    end

    // cfg_in: synchronous to clk, a plain register.
    logic cfg_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) cfg_q <= 1'b0;
        else        cfg_q <= cfg_in;
    end

    logic st_q, ack_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st_q  <= 1'b0;
            ack_q <= 1'b0;
        end else begin
            st_q  <= req_s2 & cfg_q;
            ack_q <= req_s2;
        end
    end
    assign status  = st_q;
    assign ack_out = ack_q;

endmodule
