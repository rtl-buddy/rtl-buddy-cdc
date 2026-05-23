// Positive-case fixture for the `(* cdc_static *)` attribute
// (issue #173). Configuration / mode bits are programmed once at
// boot and then held constant during the operating window; they
// are structurally cross-domain crossings without a synchroniser,
// but the metastability failure mode CDC-001 / CDC-004 target
// cannot occur because the value is not transitioning.
//
// Both the 1-bit `cfg_mode_q` and the 4-bit `cfg_data_q` registers
// are annotated `(* cdc_static *)`. The dst-domain consumer
// (`obs_q`) samples them on `dst_clk` with NO sync chain — a depth-1
// 1-bit consumer would normally trip CDC-001, and a 4-bit consumer
// without gating / gray-coding would normally trip CDC-004. The
// attribute on the source flops suppresses both rules.
//
// Paired with `bad_quasi_static_unmarked` (same RTL, no attribute):
// removing the annotation makes CDC-001 and CDC-004 fire as expected.

module marked_quasi_static (
    input  logic        cfg_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        cfg_we,
    input  logic        cfg_mode_in,
    input  logic [3:0]  cfg_data_in,
    output logic        obs_mode,
    output logic [3:0]  obs_data
);

    // Quasi-static configuration registers. The user asserts via the
    // attribute that these are programmed once and then held — so
    // dst-side sampling without a synchroniser is coherent.
    (* cdc_static *) logic       cfg_mode_q;
    (* cdc_static *) logic [3:0] cfg_data_q;

    always_ff @(posedge cfg_clk or negedge rst_n) begin
        if (!rst_n) begin
            cfg_mode_q <= 1'b0;
            cfg_data_q <= 4'd0;
        end else if (cfg_we) begin
            cfg_mode_q <= cfg_mode_in;
            cfg_data_q <= cfg_data_in;
        end
    end

    // Destination-side samplers in dst_clk — depth-1, multi-bit,
    // ungated. Would fire CDC-001 (1-bit) + CDC-004 (4-bit) without
    // the attribute; expected silent here.
    logic       obs_mode_q;
    logic [3:0] obs_data_q;

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            obs_mode_q <= 1'b0;
            obs_data_q <= 4'd0;
        end else begin
            obs_mode_q <= cfg_mode_q;
            obs_data_q <= cfg_data_q;
        end
    end

    assign obs_mode = obs_mode_q;
    assign obs_data = obs_data_q;

endmodule
