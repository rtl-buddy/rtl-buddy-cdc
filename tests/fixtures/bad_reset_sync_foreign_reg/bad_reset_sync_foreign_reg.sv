// Foreign-domain resets that are NOT synchronised (rtl-buddy-cdc#367).
//
// Three src_clk registers each drive dst_clk async resets. RDC-001
// exempts the flops of a recognised reset synchroniser, and nothing
// else; this pins what still fires:
//
// - `a1_rst_n` -> one flop `single` (D tied to 1) whose Q resets
//   `q1`. One stage is not a synchroniser: RDC-001 on `single`.
// - `a2_rst_n` -> a two-flop chain whose head loads the *asserted*
//   value (D tied to 0 on an active-low reset). That chain never
//   deasserts (RDC-007) and is not a synchroniser: RDC-001 on it.
// - `a3_rst_n` -> a correct two-flop synchroniser (silent) and, on the
//   raw reset, an ordinary data flop `q3_raw`: RDC-001 is reported on
//   `q3_raw` and lists only it.
//
// Paired with `good_reset_sync_foreign_reg`.

module bad_reset_sync_foreign_reg (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q1,
    output logic q2,
    output logic q3,
    output logic q3_raw
);

    logic a1_rst_n, a2_rst_n, a3_rst_n;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            a1_rst_n <= 1'b0;
            a2_rst_n <= 1'b0;
            a3_rst_n <= 1'b0;
        end else begin
            a1_rst_n <= 1'b1;
            a2_rst_n <= 1'b1;
            a3_rst_n <= 1'b1;
        end
    end

    // Single stage.
    logic single;
    always_ff @(posedge dst_clk or negedge a1_rst_n) begin
        if (!a1_rst_n) single <= 1'b0;
        else           single <= 1'b1;
    end
    always_ff @(posedge dst_clk or negedge single) begin
        if (!single) q1 <= 1'b0;
        else         q1 <= d_in;
    end

    // Two stages, head loads the asserted value.
    logic w0, w1;
    always_ff @(posedge dst_clk or negedge a2_rst_n) begin
        if (!a2_rst_n) begin
            w0 <= 1'b0;
            w1 <= 1'b0;
        end else begin
            w0 <= 1'b0;
            w1 <= w0;
        end
    end
    always_ff @(posedge dst_clk or negedge w1) begin
        if (!w1) q2 <= 1'b0;
        else     q2 <= d_in;
    end

    // Correct synchroniser, plus an ordinary flop on the raw reset.
    logic s0, s1;
    always_ff @(posedge dst_clk or negedge a3_rst_n) begin
        if (!a3_rst_n) begin
            s0 <= 1'b0;
            s1 <= 1'b0;
        end else begin
            s0 <= 1'b1;
            s1 <= s0;
        end
    end
    always_ff @(posedge dst_clk or negedge s1) begin
        if (!s1) q3 <= 1'b0;
        else     q3 <= d_in;
    end
    always_ff @(posedge dst_clk or negedge a3_rst_n) begin
        if (!a3_rst_n) q3_raw <= 1'b0;
        else           q3_raw <= d_in;
    end

endmodule
