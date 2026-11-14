// Single-bit captures that are NOT qualified by a handshake
// (rtl-buddy-cdc#368).
//
// CDC-001 accepts a 1-bit crossing captured under an enable synchronised
// from its own source domain, from a source held until a synced-back
// acknowledge returns (`good_handshake_single_bit`). Each capture here
// breaks exactly one of those conditions and must still fire CDC-001:
//
// - `f_free`: enable from a free-running dst_clk counter.
// - `f_unsync`: enable from a single-flop capture of the request toggle
//   (`u1` is read twice, so it is no synchroniser; `u1` fires too).
// - `f_third`: enable from a toggle synchronised from a third clock.
// - `f_dsel`: a mux that picks between two held source bits on the
//   synchronised edge and otherwise samples the other: no hold.
// - `f_open`: correct synchronised enable, but the source reloads on
//   every `load` with no acknowledge (open loop). Its 8-bit sibling
//   `d_open` is CDC-012's (warning); the 1-bit capture stays CDC-001.
//
// The first four share one closed-loop handshake (`tog_a`, ack
// `a1`/`a2`), so only the named condition differs from the good shape.

module bad_handshake_single_bit (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic       oth_clk,
    input  logic       load,
    input  logic       en_in,
    input  logic [7:0] data_in,
    input  logic       flag_in,
    output logic       f_free,
    output logic       f_unsync,
    output logic       f_third,
    output logic       f_dsel,
    output logic       f_open,
    output logic [7:0] d_open
);

    // ---- closed-loop handshake (source side) -----------------------
    logic tog_a = 1'b0;
    logic a1, a2;
    logic g1, g2, g4, g5, g6;
    wire  idle = (a2 == tog_a);
    always_ff @(posedge src_clk) begin
        a1   <= s3;
        a2   <= a1;
        if (load && idle) begin
            g1 <= flag_in;
            g2 <= ~flag_in;
            g4 <= flag_in & en_in;
            g5 <= flag_in | en_in;
            g6 <= en_in;
            tog_a <= ~tog_a;
        end
    end

    // ---- closed-loop handshake (destination side) ------------------
    logic s1, s2, s3;
    always_ff @(posedge dst_clk) begin
        s1 <= tog_a;
        s2 <= s1;
        s3 <= s2;
    end

    // Free-running enable.
    logic [2:0] cnt = 3'd0;
    always_ff @(posedge dst_clk) begin
        cnt <= cnt + 3'd1;
        if (cnt[2]) f_free <= g1;
    end

    // Enable from a single-flop capture of the toggle.
    logic u1, u2;
    always_ff @(posedge dst_clk) begin
        u1 <= tog_a;
        u2 <= u1;
        if (u1 ^ u2) f_unsync <= g2;
    end

    // Enable synchronised from a third clock.
    logic t_o = 1'b0;
    always_ff @(posedge oth_clk) t_o <= ~t_o;
    logic o1, o2, o3;
    always_ff @(posedge dst_clk) begin
        o1 <= t_o;
        o2 <= o1;
        o3 <= o2;
        if (o2 ^ o3) f_third <= g4;
    end

    // Data-select mux on the synchronised edge: no hold.
    always_ff @(posedge dst_clk)
        f_dsel <= (s2 ^ s3) ? g5 : g6;

    // ---- open-loop toggle transfer ---------------------------------
    logic       tog_o = 1'b0;
    logic       flag_o;
    logic [7:0] data_o;
    always_ff @(posedge src_clk)
        if (load) begin
            flag_o <= flag_in;
            data_o <= data_in;
            tog_o  <= ~tog_o;
        end
    logic p1, p2, p3;
    always_ff @(posedge dst_clk) begin
        p1 <= tog_o;
        p2 <= p1;
        p3 <= p2;
        if (p2 ^ p3) begin
            f_open <= flag_o;
            d_open <= data_o;
        end
    end

endmodule
