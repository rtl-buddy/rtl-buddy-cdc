// Positive fixture: a clock-pair set_false_path written in one direction
// only, with crossings in both directions (rtl-buddy-cdc#331).
//
// The SDC declares no clock groups; the only relation between ck_a and
// ck_b is `set_false_path -from [get_clocks ck_a] -to [get_clocks ck_b]`.
// A clock-pair false path is read as declaring the PAIR asynchronous
// (README "SDC support": equivalent to declaring A and B async), so both
// directions are checked, not just the one written. Here both directions
// land in a 2FF synchroniser: two async crossings, no finding.
//
//   [a_q] ck_a ──2FF──▶ ck_b      (the false-path direction)
//   [b_q] ck_b ──2FF──▶ ck_a      (the reverse direction)

module good_false_path_one_direction (
    input  logic ck_a,
    input  logic ck_b,
    input  logic rst_n,
    input  logic d_a,
    input  logic d_b,
    output logic q_a,
    output logic q_b
);

    logic a_q, b_q;
    always_ff @(posedge ck_a or negedge rst_n) begin
        if (!rst_n) a_q <= 1'b0;
        else        a_q <= d_a;
    end
    always_ff @(posedge ck_b or negedge rst_n) begin
        if (!rst_n) b_q <= 1'b0;
        else        b_q <= d_b;
    end

    // ck_a → ck_b: 2FF synchroniser.
    logic b_s1, b_s2;
    always_ff @(posedge ck_b or negedge rst_n) begin
        if (!rst_n) begin
            b_s1 <= 1'b0;
            b_s2 <= 1'b0;
        end else begin
            b_s1 <= a_q;
            b_s2 <= b_s1;
        end
    end
    assign q_b = b_s2;

    // ck_b → ck_a: 2FF synchroniser.
    logic a_s1, a_s2;
    always_ff @(posedge ck_a or negedge rst_n) begin
        if (!rst_n) begin
            a_s1 <= 1'b0;
            a_s2 <= 1'b0;
        end else begin
            a_s1 <= b_q;
            a_s2 <= a_s1;
        end
    end
    assign q_a = a_s2;

endmodule
