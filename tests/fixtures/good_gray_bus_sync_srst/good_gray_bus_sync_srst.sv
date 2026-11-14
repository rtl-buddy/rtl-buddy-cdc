// Gray pointer into a multi-bit 2FF synchroniser whose stages carry a
// synchronous reset (rtl-buddy-cdc#366).
//
// `if (rst) begin meta <= '0; q <= '0; end` lowers, after `proc`, to a
// constant-leg `$mux` in front of each stage's `D`, so stage 1's `Q`
// reaches stage 2 through that mux rather than directly. The reset only
// forces the lanes to a constant and moves no data between them: the
// chain is still a lane-aligned multi-bit synchroniser, and the gray
// source keeps CDC-004 silent. Two pointers cover both reset sources a
// synchroniser stage sees in practice: `gray_p` into stages reset by
// the primary reset port `rst_b`, and `gray_l` into stages reset by a
// local reset synchronised into dst_clk. The `.optdff.json` netlist is
// the same design after `opt_dff`, which folds each reset into a
// `$sdff`.
//
// Paired with `good_gray_bus_sync_chain` (no reset) and with the
// negative `bad_bus_crossing`.

module good_gray_bus_sync_srst (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_a,
    input  logic        rst_b,
    input  logic        incr,
    output logic [3:0]  q_p,
    output logic [3:0]  q_l
);

    logic [3:0] bin;
    logic [3:0] gray_p;
    logic [3:0] gray_l;
    logic [3:0] bin_next;

    assign bin_next = bin + 4'(incr);

    always_ff @(posedge src_clk) begin
        bin    <= bin_next;
        gray_p <= bin_next ^ (bin_next >> 1);
        gray_l <= bin_next ^ (bin_next >> 1);
        if (rst_a) begin
            bin    <= 4'd0;
            gray_p <= 4'd0;
            gray_l <= 4'd0;
        end
    end

    // Stages reset by the primary reset port.
    logic [3:0] meta_p;
    always_ff @(posedge dst_clk) begin
        meta_p <= gray_p;
        q_p    <= meta_p;
        if (rst_b) begin
            meta_p <= 4'd0;
            q_p    <= 4'd0;
        end
    end

    // Stages reset by a reset synchronised into dst_clk.
    logic rst_s0, rst_s1;
    always_ff @(posedge dst_clk) begin
        rst_s0 <= rst_b;
        rst_s1 <= rst_s0;
    end

    logic [3:0] meta_l;
    always_ff @(posedge dst_clk) begin
        meta_l <= gray_l;
        q_l    <= meta_l;
        if (rst_s1) begin
            meta_l <= 4'd0;
            q_l    <= 4'd0;
        end
    end

endmodule
