// Positive fixture for CDC-024 (issue #323): every top-level output
// carries a set_output_delay contract whose receiving clock is related
// to the clock that launches it, so CDC-024 must stay silent even
// though most of the contract clock names differ from the launch clock.
//
// q_reg is contracted to clk_a itself (same domain). q_comb, a
// combinational cone of two clk_a flops, is contracted to vclk_a, a
// virtual clock the SDC never declares asynchronous to clk_a. q_bus is
// contracted to clk_a_fwd, a generated clock of clk_a forwarded out on
// clk_fwd (a source-synchronous interface). q_b is launched and
// contracted in a second clock, clk_b. The SDC does declare clk_a and
// clk_b asynchronous, so the rule runs against real async groups.

module good_output_sync_contract (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       d0,
    input  logic       d1,
    input  logic [3:0] d_bus,
    input  logic       d_b,
    output logic       q_reg,
    output logic       q_comb,
    output logic [3:0] q_bus,
    output logic       q_b,
    output logic       clk_fwd
);

    logic r0;
    logic r1;

    always_ff @(posedge clk_a) begin
        q_reg <= d0;
        r0    <= d0;
        r1    <= d1;
        q_bus <= d_bus;
    end

    always_ff @(posedge clk_b) begin
        q_b <= d_b;
    end

    assign q_comb  = r0 ^ r1;
    assign clk_fwd = clk_a;

endmodule
