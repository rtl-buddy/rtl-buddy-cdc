// Positive counterpart to bad_unconstrained_input_two_domains.
//
// The SDC types `in` against `clk_a` via `set_input_delay -clock` and
// the RTL adds a 2FF synchronizer on the `clk_b` side, so the
// methodology gap CDC-011 surfaced is fully resolved: CDC-011 stays
// silent (port is typed), CDC-001 stays silent (chain depth >= 2 on
// the foreign-domain capture). The textbook fix shape.

module good_unconstrained_input_two_domains_typed (
    input  logic clk_a,
    input  logic clk_b,
    input  logic in,
    output logic q_a,
    output logic q_b
);

    // Native-domain capture: no synchronizer needed.
    always_ff @(posedge clk_a) q_a <= in;

    // Foreign-domain capture: 2FF synchronizer.
    (* cdc_sync *) logic sync_meta;
    logic                sync_q;
    always_ff @(posedge clk_b) sync_meta <= in;
    always_ff @(posedge clk_b) sync_q    <= sync_meta;
    assign q_b = sync_q;

endmodule
