// Negative fixture: a destination-domain signal reaches the source
// payload's value, but nothing holds the payload (rtl-buddy-cdc#369).
//
// clk_a and clk_b are asynchronous. clk_a loads an 8-bit payload on
// `load` and toggles a request; clk_b synchronises the toggle (2FF plus
// a third stage for edge detection) and captures the payload on each
// edge, so the bus crossing is gated (no CDC-004). A mode bit from
// clk_b is synchronised into clk_a (2FF) and inverts the payload as it
// is loaded. That is a path from clk_b back into the payload register,
// but it only changes *what* is stored: `load` can fire again while
// clk_b is still sampling, so the payload is not held. Expected
// verdict: CDC-012 on data_a, nothing else.
//
//   clk_b [m_b] ──2FF──▶ clk_a [m2] ──value──▶ [data_a] ──gated──▶ clk_b
//   clk_a [tog_a] ──3FF──▶ clk_b (edge → load enable)

module bad_datahold_dst_mode_bit (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       load,
    input  logic       mode_b,
    input  logic [7:0] data_in,
    output logic [7:0] data_out
);

    // ---- clk_b: a mode bit unrelated to the handshake ----
    logic m_b;
    always_ff @(posedge clk_b) m_b <= mode_b;

    // ---- clk_a: mode synchroniser, payload, request toggle ----
    logic       m1, m2, tog_a;
    logic [7:0] data_a;
    always_ff @(posedge clk_a) begin
        m1 <= m_b;
        m2 <= m1;
        if (load) begin
            data_a <= m2 ? ~data_in : data_in;
            tog_a  <= ~tog_a;
        end
    end

    // ---- clk_b: toggle synchroniser and gated capture ----
    logic s1, s2, s3;
    always_ff @(posedge clk_b) begin
        s1 <= tog_a;
        s2 <= s1;
        s3 <= s2;
        if (s2 ^ s3) data_out <= data_a;
    end

endmodule
