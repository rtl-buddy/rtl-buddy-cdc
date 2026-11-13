// Positive fixture: a quasi-static bus crossing acknowledged by an
// object-scoped set_false_path -through (rtl-buddy-cdc#331).
//
// src_clk and dst_clk are asynchronous. A 4-bit configuration register,
// cfg_q, written in src_clk, is routed through the pass-through
// instance u_bridge and captured by a single 4-bit dst_clk register: on
// its own a CDC-004 multi-bit crossing. The SDC pins the path with
// `set_false_path -through [get_pins u_bridge/cfg_o]`. Every path from
// cfg_q to the capture register passes that pin, so the statement covers
// the crossing and, by pinning the path, acknowledges it: CDC-004 moves
// to the suppressed list with the SDC statement as the reason. The run
// is clean (exit 0) and reports one suppressed finding.
//
//   [cfg_q] src_clk ─4b─▶ u_bridge/cfg_o ─4b─▶ [cfg_cap] dst_clk
//                         └ set_false_path -through ⇒ CDC-004 suppressed

module cfg_bridge (
    input  logic [3:0] cfg_i,
    output logic [3:0] cfg_o
);
    assign cfg_o = cfg_i;
endmodule

module good_false_path_through_bus (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       rst_n,
    input  logic       we,
    input  logic [3:0] wdata,
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

endmodule
