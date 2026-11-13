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


// Multi-bit meta_flop (rtl-buddy-cdc#331): a WIDTH-bit register whose
// lanes resolve independently, as the WIDTH separate flops of a real
// register do. Each lane follows `meta_flop`'s rule on its own: if
// its `d` bit changed since the previous edge, it rolls the dice and
// may capture a random bit (so a changing lane lands either on time
// or one cycle late, never in step with the other lanes by design).
// One `$dist_uniform` stream is shared by the lanes, drawn in lane
// order.
//
// The seed is `SEED`, XOR'd with the `+META_SEED=<n>` plusarg when
// given, so a test can rerun one compiled image under several
// injection sequences. `meta_flop` itself ignores the plusarg, which
// keeps the older oracle tests bit-for-bit unchanged.
//
// `meta_flop_synth.sv` defines the same module names as plain flops;
// the DUTs include it instead of this file when Yosys builds the
// netlist the analyzer reads, so both sides see one source.
module meta_flop_bus #(
    parameter int unsigned WIDTH    = 2,
    parameter int unsigned RATE_PCT = 80,
    parameter int unsigned SEED     = 32'hC0FFEE02
) (
    input  logic             clk,
    input  logic [WIDTH-1:0] d,
    output logic [WIDTH-1:0] q
);
    logic [WIDTH-1:0] d_prev;
    integer seed_state;
    integer plus_seed;

    initial begin
        seed_state = SEED;
        if ($value$plusargs("META_SEED=%d", plus_seed))
            seed_state = SEED ^ plus_seed;
        q      = '0;
        d_prev = '0;
    end

    always_ff @(posedge clk) begin
        for (int i = 0; i < WIDTH; i++) begin
            if (d[i] !== d_prev[i] &&
                $dist_uniform(seed_state, 0, 99) < RATE_PCT)
                q[i] <= $dist_uniform(seed_state, 0, 1);
            else
                q[i] <= d[i];
        end
        d_prev <= d;
    end
endmodule


// `meta_flop_bus` with a load enable: a capture register that loads
// `d` only while `en` is high and holds otherwise. Injection applies
// only to a lane that loads a value which changed since the previous
// edge — with `en` low the flop recirculates its own `q`, which is
// synchronous and cannot go metastable. `d_prev` tracks `d` on every
// edge, enabled or not, so a load of a payload that has been stable
// for a full destination period is always clean.
module meta_flop_bus_en #(
    parameter int unsigned WIDTH    = 2,
    parameter int unsigned RATE_PCT = 80,
    parameter int unsigned SEED     = 32'hC0FFEE03
) (
    input  logic             clk,
    input  logic             en,
    input  logic [WIDTH-1:0] d,
    output logic [WIDTH-1:0] q
);
    logic [WIDTH-1:0] d_prev;
    integer seed_state;
    integer plus_seed;

    initial begin
        seed_state = SEED;
        if ($value$plusargs("META_SEED=%d", plus_seed))
            seed_state = SEED ^ plus_seed;
        q      = '0;
        d_prev = '0;
    end

    always_ff @(posedge clk) begin
        if (en) begin
            for (int i = 0; i < WIDTH; i++) begin
                if (d[i] !== d_prev[i] &&
                    $dist_uniform(seed_state, 0, 99) < RATE_PCT)
                    q[i] <= $dist_uniform(seed_state, 0, 1);
                else
                    q[i] <= d[i];
            end
        end
        d_prev <= d;
    end
endmodule


// Reset-aware meta_flop. `arst_n` is an active-low async reset.
//
// `ARST_IS_ASYNC` is an explicit parameter set by the DUT:
//   1 → arst_n comes from a foreign clock domain or directly from
//       a top-level port; recovery/removal violations are possible
//       on every deassertion edge.
//   0 → arst_n is driven by a flop in this clk's own domain
//       (typically the tail of a reset sync chain); deassertion
//       edges land on clk edges and no recovery violation occurs.
//
// Modelling at this level mirrors the analyzer's structural
// decision: RDC-001 flags a flop iff its ARST source is in a
// foreign clock domain. The DUT declares ARST_IS_ASYNC the same
// way it declares which flops are reset synchronisers.
//
// On assertion (arst_n falls), `q` goes to 0 immediately. On
// deassertion (arst_n rises), if ARST_IS_ASYNC and a deassertion
// is pending, the first clk edge after release rolls for
// X-injection (probability RATE_PCT/100). Otherwise the flop
// updates cleanly.
module meta_flop_arst #(
    parameter int unsigned RATE_PCT      = 50,
    parameter int unsigned SEED          = 32'hDEADC0DE,
    parameter bit          ARST_IS_ASYNC = 1
) (
    input  logic clk,
    input  logic arst_n,
    input  logic d,
    output logic q
);
    integer seed_state;
    logic   pending_inject;
    int     r;

    initial begin
        seed_state     = SEED;
        q              = 1'b0;
        pending_inject = 1'b0;
    end

    // Detect deassertion: a posedge on arst_n (asynchronous wrt clk
    // when ARST_IS_ASYNC). Mark for injection on the next clk
    // edge.
    always @(posedge arst_n) begin
        if (ARST_IS_ASYNC) pending_inject = 1'b1;
    end

    always_ff @(posedge clk or negedge arst_n) begin
        if (!arst_n) begin
            q              <= 1'b0;
            pending_inject <= 1'b0;
        end else if (pending_inject) begin
            r = $dist_uniform(seed_state, 0, 99);
            if (r < RATE_PCT) q <= $dist_uniform(seed_state, 0, 1);
            else              q <= d;
            pending_inject <= 1'b0;
        end else begin
            q <= d;
        end
    end
endmodule


// Reset-aware safe_flop (async low). No metastability injection;
// used for source-domain reset distribution where the reset edge
// is known to meet timing on its own clock.
module safe_flop_arst (
    input  logic clk,
    input  logic arst_n,
    input  logic d,
    output logic q
);
    initial q = 1'b0;
    always_ff @(posedge clk or negedge arst_n)
        if (!arst_n) q <= 1'b0;
        else         q <= d;
endmodule

`endif
