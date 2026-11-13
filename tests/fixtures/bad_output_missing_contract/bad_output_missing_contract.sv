// Negative fixture for CDC-025 (issue #329): an SDC that types some
// top-level outputs with set_output_delay -clock but misses others. Once
// the user has started typing outputs, a data output launched from
// clocked logic with no contract is likely an omission, so CDC-025 warns
// on it, naming its launch domain.
//
// q_a is contracted and stays silent. q_b (a clk_b register), q_bus (a
// 4-bit clk_a register, one finding for the whole bus) and q_pair (a clk_a
// bit beside a clk_b bit, which no single contract can describe) are not,
// and each fires once. The exclusions stay silent too: clk_fwd forwards a
// clock port, clk_gated gates one, div_o is a divider register that also
// clocks internal logic, tie_o is constant and thru_o is a combinational
// feed-through of an input. CDC-025 must fire exactly three warnings, and
// nothing else may fire.

module bad_output_missing_contract (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       d_a,
    input  logic       d_b,
    input  logic [3:0] bus_a,
    input  logic       en_b,
    output logic       q_a,
    output logic       q_b,
    output logic [3:0] q_bus,
    output logic [1:0] q_pair,
    output logic       clk_fwd,
    output logic       clk_gated,
    output logic       div_o,
    output logic       tie_o,
    output logic       thru_o
);

    logic pa;
    logic pb;
    logic en_q;
    logic div_q;
    logic slow_q;
    logic gated_q;

    always_ff @(posedge clk_a) begin
        q_a   <= d_a;
        q_bus <= bus_a;
        pa    <= d_a;
        div_q <= ~div_q;
    end

    always_ff @(posedge clk_b) begin
        q_b  <= d_b;
        pb   <= d_b;
        en_q <= en_b;
    end

    // Flops clocked by the divider and by the gated clock, so both drive
    // the clock network as well as an output.
    always_ff @(posedge div_q) slow_q <= d_a;
    always_ff @(posedge clk_gated) gated_q <= d_b;

    assign q_pair    = {pb, pa};
    assign clk_fwd   = clk_a;
    assign clk_gated = clk_b & en_q;
    assign div_o     = div_q;
    assign tie_o     = 1'b0;
    assign thru_o    = d_a;

endmodule
