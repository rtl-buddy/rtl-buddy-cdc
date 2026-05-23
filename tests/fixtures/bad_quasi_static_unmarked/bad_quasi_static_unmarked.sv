// Negative counterpart to `marked_quasi_static` (issue #173):
// structurally identical configuration-register pattern, but the
// `(* cdc_static *)` attribute has been omitted from both source
// flops.
//
// Without the attribute the analyzer cannot tell a held-after-boot
// register apart from a routinely-toggling one — so it must treat
// the depth-1 single-bit crossing as CDC-001 and the ungated 4-bit
// bus crossing as CDC-004. The paired test pins both findings.
//
// This fixture exists to confirm two things:
//
// 1. The analyzer fires the expected rules in the absence of the
//    attribute — i.e. the marked fixture's clean result is not
//    accidentally trivial.
// 2. The marked fixture and this fixture are *the same RTL* modulo
//    the annotation, so the attribute is doing exactly the work
//    claimed.

module bad_quasi_static_unmarked (
    input  logic        cfg_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        cfg_we,
    input  logic        cfg_mode_in,
    input  logic [3:0]  cfg_data_in,
    output logic        obs_mode,
    output logic [3:0]  obs_data
);

    // Same RTL as marked_quasi_static, with the (* cdc_static *)
    // annotations stripped — so CDC-001 / CDC-004 fire as normal.
    logic       cfg_mode_q;
    logic [3:0] cfg_data_q;

    always_ff @(posedge cfg_clk or negedge rst_n) begin
        if (!rst_n) begin
            cfg_mode_q <= 1'b0;
            cfg_data_q <= 4'd0;
        end else if (cfg_we) begin
            cfg_mode_q <= cfg_mode_in;
            cfg_data_q <= cfg_data_in;
        end
    end

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
