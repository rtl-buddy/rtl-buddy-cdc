// Behavioural metastability injection library for the fuzz sim oracle.
//
// `meta_flop` is a drop-in replacement for a single-bit flop whose
// `D` is potentially crossing clock domains. On a clock edge where
// `D` differs from the previously-sampled value, with probability
// `RATE_PCT/100` the module emits a random bit instead of the
// freshly-sampled `D` — modelling the analog outcome of sampling a
// signal during the setup/hold window.
//
// This is **not** a faithful analog metastability model — it cannot
// be in a digital simulator. It is the Litterick/Cummings
// "behavioural injection" methodology: sample-time uncertainty
// modelled as a stochastic flip on the cycle a transition crosses.
// Repeated runs with different seeds expose CDC failure modes that
// a clean digital simulation would silently sweep through.
//
// `safe_flop` is a parameter-compatible non-injecting flop, used
// for source-domain flops where the input is synchronous and
// metastability does not apply.

`ifndef META_FLOP_LIB_SV
`define META_FLOP_LIB_SV

module meta_flop #(
    parameter int unsigned RATE_PCT = 30,
    parameter int unsigned SEED     = 32'hC0FFEE01
) (
    input  logic clk,
    input  logic d,
    output logic q
);
    logic d_prev;
    int   r;
    integer seed_state;

    initial begin
        seed_state = SEED;
        q          = 1'b0;
        d_prev     = 1'b0;
    end

    always_ff @(posedge clk) begin
        if (d !== d_prev) begin
            // Transition detected — roll the dice. Injection picks
            // between the new value, the old value, and a random
            // bit. The bias here is harsh on purpose: we want sim
            // failures to surface on the unsynchronised crossings
            // even in short runs.
            r = $dist_uniform(seed_state, 0, 99);
            if (r < RATE_PCT) begin
                q <= $dist_uniform(seed_state, 0, 1);
            end else begin
                q <= d;
            end
        end else begin
            q <= d;
        end
        d_prev <= d;
    end
endmodule


module safe_flop #(
    parameter int unsigned SEED = 32'h0
) (
    input  logic clk,
    input  logic d,
    output logic q
);
    initial q = 1'b0;
    always_ff @(posedge clk) q <= d;
endmodule

`endif
