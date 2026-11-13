// Negative fixture: an object-scoped set_false_path -through that
// covers one bus crossing but not a second one (rtl-buddy-cdc#331).
//
// Same as good_false_path_through_bus (cfg_q routed through u_bridge,
// `set_false_path -through [get_pins u_bridge/cfg_o]`), plus a second
// 4-bit src_clk register, stat_q, captured by its own single dst_clk
// register without passing u_bridge. A -through statement covers only
// the paths that pass its pin, so it acknowledges the cfg crossing
// (CDC-004 suppressed) and leaves the stat crossing alone: CDC-004 fires
// exactly once, on stat_q → stat_cap.
//
//   [cfg_q]  src_clk ─4b─▶ u_bridge/cfg_o ─4b─▶ [cfg_cap]   CDC-004 suppressed
//   [stat_q] src_clk ─4b───────────────────▶ [stat_cap]  → CDC-004

module cfg_bridge (
    input  logic [3:0] cfg_i,
    output logic [3:0] cfg_o
);
    assign cfg_o = cfg_i;
endmodule

module bad_false_path_through_partial (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       rst_n,
    input  logic       we,
    input  logic [3:0] wdata,
    input  logic [3:0] stat_in,
    output logic [3:0] q_stat,
    output logic [3:0] q_cfg
);

    // Configuration register, written rarely in src_clk.
    logic [3:0] cfg_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)  cfg_q <= 4'h0;
        else if (we) cfg_q <= wdata;
    end

    logic [3:0] cfg_w;
    cfg_bridge u_bridge (.cfg_i(cfg_q), .cfg_o(cfg_w));

    // Single capture register in dst_clk: structurally CDC-004.
    logic [3:0] cfg_cap;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) cfg_cap <= 4'h0;
        else        cfg_cap <= cfg_w;
    end
    assign q_cfg = cfg_cap;

    // BUG: a second bus that does not pass u_bridge.
    logic [3:0] stat_q, stat_cap;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)  stat_q <= 4'h0;
        else         stat_q <= stat_in;
    end
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) stat_cap <= 4'h0;
        else        stat_cap <= stat_q;
    end
    assign q_stat = stat_cap;

endmodule
