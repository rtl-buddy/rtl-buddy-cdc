// Negative fixture for CDC-024 (issue #323): top-level outputs launched
// in clk_a whose set_output_delay contract names a receiving clock the
// SDC declares asynchronous to clk_a. The capture happens outside the
// design, so no internal flop receives these outputs and, before CDC-024,
// no crossing or finding existed at all.
//
// One output per cone shape: q_reg is a directly registered output,
// q_comb a combinational cone of two clk_a flops (both contracted to the
// virtual clock vclk_b), q_bus a 4-bit registered vector, and q_mix a
// 4-bit vector whose upper two bits come from clk_a and lower two from
// clk_b (both contracted to clk_b, so only the clk_a half is async).
// CDC-024 must fire exactly once per output port, and nothing else may
// fire.

module bad_output_async_contract (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       d0,
    input  logic       d1,
    input  logic [3:0] d_bus,
    input  logic [1:0] d_b,
    output logic       q_reg,
    output logic       q_comb,
    output logic [3:0] q_bus,
    output logic [3:0] q_mix
);

    logic       r0;
    logic       r1;
    logic [1:0] ra;
    logic [1:0] rb;

    always_ff @(posedge clk_a) begin
        q_reg <= d0;
        r0    <= d0;
        r1    <= d1;
        q_bus <= d_bus;
        ra    <= d_bus[1:0];
    end

    always_ff @(posedge clk_b) begin
        rb <= d_b;
    end

    assign q_comb = r0 ^ r1;
    assign q_mix  = {ra, rb};

endmodule
