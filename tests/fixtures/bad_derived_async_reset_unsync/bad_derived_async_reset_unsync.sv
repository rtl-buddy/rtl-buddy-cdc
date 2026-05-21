// Negative fixture for derived async reset deassertion.
//
// The reset source is selected intentionally by a mux, but the selected
// reset feeds a flop's asynchronous clear pin directly. The consumer
// clock has no local reset synchronizer, so reset deassertion can occur
// asynchronously relative to clk.

module bad_derived_async_reset_unsync (
    input  logic clk,
    input  logic global_rst_n,
    input  logic block_rst_n,
    input  logic use_block_rst,
    input  logic d_in,
    output logic q_out
);

    logic selected_rst_n;
    assign selected_rst_n = use_block_rst ? block_rst_n : global_rst_n;

    always_ff @(posedge clk or negedge selected_rst_n) begin
        if (!selected_rst_n) q_out <= 1'b0;
        else                 q_out <= d_in;
    end

endmodule
