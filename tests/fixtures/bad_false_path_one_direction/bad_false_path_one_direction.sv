// Negative fixture: a clock-pair set_false_path written in one direction
// only, with the reverse crossing unsynchronised (rtl-buddy-cdc#331).
//
// Same SDC as good_false_path_one_direction: only
// `set_false_path -from [get_clocks ck_a] -to [get_clocks ck_b]`. The
// clock pair is read as asynchronous in both directions, so the reverse
// crossing ck_b → ck_a, captured by a single flop, is checked like any
// other: CDC-001 fires exactly once (b_q → a_cap). Reading the false
// path as one-way would leave ck_b → ck_a timed and hide the missing
// synchroniser; the pairwise reading reports it. The written direction,
// ck_a → ck_b, keeps its 2FF synchroniser.
//
//   [a_q] ck_a ──2FF──▶ ck_b      (the false-path direction)
//   [b_q] ck_b ──1FF──▶ ck_a      → CDC-001

module bad_false_path_one_direction (
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

    // ck_b → ck_a: BUG, a single capture flop.
    logic a_cap;
    always_ff @(posedge ck_a or negedge rst_n) begin
        if (!rst_n) a_cap <= 1'b0;
        else        a_cap <= b_q;
    end
    assign q_a = a_cap;

endmodule
