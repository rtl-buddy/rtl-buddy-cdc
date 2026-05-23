# Fuzzer + simulation oracle for CDC/RDC rule validation

**Tracking issue:** TBD (linked from umbrella rtl-buddy-cdc#188).
**Status:** proposal.
**Suggested deliverable:** new `tests/fuzz/` test subtree and an
optional `tests/sim/` harness, both gated behind a marker so the
default `pytest` run stays fast.

## 1. Motivation

The existing test surface is a paired bad/good fixture per rule:
hand-authored RTL, hand-authored expected verdict. This is precise
but slow to grow, and it can only catch bugs the author thought to
write a fixture for. The 12 gaps catalogued in
`docs/cdc-lint-coverage-survey.md` (rtl-buddy-cdc#188) are exactly
the kind of shapes a hand-authored test plan tends to miss — by
construction, no one wrote a fixture for a failure mode they didn't
think of.

Two complementary techniques address this:

- **Template-driven random generation.** Build a generator that emits
  parameterised SV from a small library of CDC failure-mode
  templates, sweeping bus widths, clock counts, sync depths, reset
  shapes, and gating idioms. The template *is* the oracle: it knows
  by construction whether a given variant should fire a given rule.
  This produces hundreds of high-quality fixtures from a few dozen
  templates and grows the corpus without growing the fixture-author
  workload. Analogous to Csmith / Yarpgen for C compiler testing and
  Verismith for HDL.

- **Behavioural simulation oracle.** Run the generated RTL in
  Icarus / Verilator with behavioural flop and gate wrappers that
  inject metastability and clock glitches the way real silicon
  would fail. Cross-reference simulation outcomes against analyzer
  findings: a circuit that *fails simulation* but is *accepted by
  the analyzer* is a candidate false-negative — a gap to close.
  Established methodology (Litterick, DVCon Europe 2006; Cummings,
  SNUG 2008 §11; Synopsys VCS `+vcs_ms_inject`).

Industry consensus is that simulation **complements** structural
lint rather than replacing it — metastability is fundamentally an
analog phenomenon, and digital sim can only approximate it via
stochastic injection. This proposal treats simulation as a
**false-negative finder**, not a verdict authority.

## 2. Architecture

Three components, each independently useful, sequenced so that each
stage delivers value before the next is built.

### 2.1 Template-driven fuzzer (`tests/fuzz/templates/`)

A Python package that emits SV modules from parameterised templates.
Each template is a Python class implementing:

```python
class Template:
    rule_id: str           # rule the template targets
    verdict: Literal["bad", "good"]
    expected_findings: list[ExpectedFinding]  # what run_all should produce

    def parameters(self) -> Iterator[dict]:
        """Yield every {param: value} combination this template
        wants to sweep — bus widths, sync depths, reset polarities,
        etc."""

    def render(self, params: dict) -> RenderedCase:
        """Return SV source + SDC + a deterministic case id."""
```

Initial template library (sized to map onto the gap survey):

| Template                              | Target rule(s)            | Variants  |
|---------------------------------------|---------------------------|-----------|
| `UnsyncedSingleBitCrossing`           | CDC-001                   | bus-width × reset-polarity × gen-clock vs. direct |
| `ShortChain`                          | CDC-002                   | depth ∈ {1, 2, 3} × `--sync-depth` ∈ {2, 3, 4} |
| `CombBetweenSourceAndSync`            | CDC-003                   | gate type × inverter parity |
| `UncodedBus`                          | CDC-004                   | bus-width × gating shape (none / mux-on-D / dffe-EN) |
| `GrayBus`                             | CDC-004 (good)            | bus-width × structural vs. `(* cdc_gray *)` |
| `Reconvergence`                       | CDC-005                   | fanout ∈ {2, 3} × disjoint vs. shared downstream cone |
| `LatchInChain`                        | CDC-017 (proposed)        | `$dlatch` position × surrounding shape |
| `PulseSyncChain`                      | CDC-013 (good — recognise)| with vs. without XOR tail |
| `OneHotDecodedCrossing`               | CDC-019 (proposed)        | N ∈ {2, 4, 8} lanes |
| `OppositeEdgeChain`                   | CDC-016                   | polarity transitions × chain depth |
| `ResetCrossing`                       | RDC-001                   | tree fanout × intermediate buffers |
| `ResetSyncDeassertionPolarity`        | RDC-007 (proposed)        | head-`D`-constant ∈ {asserted, deasserted} × polarity |
| `OpaqueGateGenerator`                 | smoke / regression        | dead code, mixed-domain noise |

Each template emits a `RenderedCase` containing `module.sv`,
`module.sdc`, and a JSON sidecar carrying the expected verdict and
parameters. Templates are deterministic given a seed.

### 2.2 Analyzer-differential harness (`tests/fuzz/test_corpus.py`)

For every rendered case:

1. Invoke Yosys to produce the JSON netlist.
2. Run `find_crossings` + `run_all_rules`.
3. Compare actual findings against the template's
   `expected_findings`.
4. Report mismatches.

Caching: the Yosys invocation is the hot path. Cache by SHA-256 of
the rendered SV+SDC so re-runs of the same case are free. Templates
are deterministic, so the cache hit rate is high once the corpus
stabilises.

Marker: `@pytest.mark.fuzz` so `pytest -m "not fuzz"` keeps default
runs fast. A separate `pytest -m fuzz` invocation runs the corpus.
CI runs both.

Coverage feedback: track per-rule fire counts across the corpus.
Templates that never fire their target rule are flagged. This is
the simplest possible coverage-guided steering — full
observation-coverage feedback is a future enhancement.

### 2.3 Simulation oracle (`tests/sim/`, opt-in)

For cases that benefit from a runtime check, wrap the generated SV
with behavioural models and run under Icarus or Verilator.

**Meta-flop wrapper.** Replace each user flop with a module that,
with configurable probability when its `D` changes within a
configurable `tsu` window of the clock edge, emits an old-value /
new-value / one-cycle-delayed-value. Default rate `~1%` per cycle
per flop, tunable per simulation run.

```systemverilog
module meta_flop #(parameter int unsigned WIDTH = 1,
                   parameter int unsigned RATE_PCT = 1)
                  (input logic clk,
                   input logic [WIDTH-1:0] d,
                   output logic [WIDTH-1:0] q);
    logic [WIDTH-1:0] d_q;
    always_ff @(posedge clk) d_q <= d;
    always_ff @(posedge clk) begin
        if ($urandom_range(0, 99) < RATE_PCT && d !== d_q)
            q <= $urandom_range(0, (1 << WIDTH) - 1);
        else
            q <= d_q;
    end
endmodule
```

**Glitchy mux/AND wrapper.** Replace `$mux`/`$and` cells on
identified clock-network paths with a wrapper that emits a runt
pulse when a control input transitions while the output is asserted.
Icarus-only — Verilator's 2-state evaluation cannot model this.

**Testbench.** Generated alongside the case. Drives the source-side
inputs with a coverage-driving stimulus (toggling at random rates
on each source clock), samples the destination outputs against a
golden model built from the template metadata. Any sampled value
that diverges from the golden trace is a "sim failure" event.

**Cross-reference.** A sim-failure for a case the analyzer accepted
is a candidate false-negative. A clean sim for a case the analyzer
flagged is *not* automatically a false-positive — simulation
sample-thinness alone cannot rule out the bug. The directionality
is asymmetric.

## 3. Implementation stages

Each stage delivers value standalone; later stages are optional
follow-ons.

**Stage 1 — Template generator + analyzer-differential harness.**
~1 week. Targets: 12 templates covering CDC-001..-006, -016 +
RDC-001/-002. Cache infrastructure, pytest marker, CI integration.
Exit criterion: corpus of ≥500 cases passing on `main`, with
template-level coverage report.

**Stage 2 — Coverage steering and expanded template library.**
Add templates for CDC-008/-009/-010/-011/-012/-013/-014/-015 and the
remaining RDC family. Bias generation toward under-covered rules
based on stage-1 coverage data. Exit criterion: every shipping rule
fires ≥10 times across the corpus on `main`.

**Stage 3 — Icarus simulation harness with meta-flop injection.**
Add `tests/sim/`, the meta-flop wrapper library, testbench
generator. Marker `@pytest.mark.sim` (opt-in only, not on default
CI). Exit criterion: for each of the known-bad templates, simulation
agrees with the analyzer's verdict on ≥80% of variants within a
single seed; cases where they disagree are triaged.

**Stage 4 — Clock-glitch injection for CDC-008 / CDC-010.**
Icarus-only. Adds the glitchy mux/AND wrappers. Exit criterion:
analyzer's CDC-008 / CDC-010 findings on the corpus correlate with
simulation glitch detection events. Validates the analyzer's
glitch model.

**Stage 5 — Gap-mining mode.** Run stages 1+3 with templates that
deliberately step outside any documented rule (e.g., G-1 latch
in chain, G-3 pulse-sync, G-12 gate-level). Any sim-failure
without an analyzer finding becomes a candidate issue. This is the
"close the gap" payoff — and the explicit motivation for building
the rest.

## 4. Risks and limits

- **Metastability injection tuning.** Too aggressive → every sim
  run fails → no signal. Too gentle → real bugs slip through. Mitigation:
  Litterick's defaults as a starting point; tune per template based on
  observed false-positive rate.
- **Verilator vs. Icarus tradeoff.** Verilator is faster but 2-state
  (no glitch hazard model). Icarus is event-driven and sees glitches
  but is slower. Route per-case: Verilator for the meta-flop
  scenarios, Icarus for clock-glitch scenarios.
- **Sim oracle is asymmetric.** A clean sim does **not** prove the
  analyzer's finding is wrong (sim sample-thinness, injection-rate
  dependence). Only sim-failures-on-accepted-cases are actionable.
  Document this explicitly in the harness output.
- **Yosys is the inner loop.** Each generated case costs a Yosys
  invocation. Cache by content hash; batch where possible.
- **Template explosion.** Cartesian-product parameter sweeps grow
  quickly. Cap per-template variant count; rely on randomised
  sampling for the long tail.
- **Coverage is sparse without steering.** Random circuits hit the
  same shapes repeatedly. Stage 2's coverage-driven steering is the
  mitigation; without it, the corpus plateaus.
- **Test infrastructure complexity.** Adds a Yosys+Icarus+Verilator
  toolchain dependency on the dev path. Mitigation: gate behind
  markers; document as opt-in in the README. The default `pytest`
  run stays Python-only.

## 5. Out of scope

- **Full random RTL generation** (Verismith / Csmith style).
  Templates suffice for CDC validation; full random RTL is a
  separate research project.
- **Cross-tool differential against commercial CDC tools.**
  Licensing and reproducibility issues. Future stage if needed.
- **Floor-plan or timing-aware simulation.** The metastability
  injection is a behavioural model, not a back-annotated one.
- **MTBF estimation.** The simulation oracle answers "did the
  circuit fail under injection?", not "what is the mean time
  between failures?" — the latter requires technology-specific τ.
- **Formal / temporal-logic verification of handshake protocols.**
  Out of scope for structural lint and out of scope here.

## 6. Success criteria

The proposal is successful if:

1. Stage 1 corpus catches a regression in `run_all_rules` that the
   hand-authored fixture suite would miss (proves the differential
   value).
2. Stage 3 simulation surfaces at least one false-negative
   candidate that maps onto a survey gap (proves the gap-mining
   value).
3. Default `pytest` runtime stays under its current budget
   (proves the marker / caching strategy works).

If stage 1 hits criterion 1 but the rest stalls, the project still
delivered: the fuzzer corpus alone is a permanent productivity
multiplier on writing new rules.
