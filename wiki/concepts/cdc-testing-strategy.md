---
title: CDC Testing Strategy
created: 2026-05-11
updated: 2026-11-13
type: concept
tags: [testing, cdc, architecture, extension-point, frontend, fuzz, sim]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# CDC Testing Strategy

## Fixture-Based Testing

Each CDC rule has at least one **bad fixture** (designed to fire it) and one **good fixture** (the textbook fix that must not fire). Fixtures are paired SystemVerilog + SDC + pre-built Yosys JSON under `tests/fixtures/`. The pairing catches false positives when a rule gets tightened — the good fixture is the regression net.

### Test Files

| Test file | Coverage |
|---|---|
| `test_good_fixtures.py` | Parametrized sweep over all good fixtures asserting zero violations |
| `test_bad_*.py` | One file per bad fixture asserting the expected rule fires and no other rule false-fires |
| `test_handshake_fixture.py` | The `ip_cdc_handshake` golden — full pipeline + CDC-002 threshold behaviour |
| `test_clock_gating.py` | ICG positive case — clock-network cells must not trip CDC-008 |
| `test_marked_user_sync.py` | `(* cdc_sync *)` / `(* synchronizer *)` / `(* async_reg *)` attribute paths |
| `test_waivers.py` | Waiver parsing, matching, end-to-end CLI exit-code suppression |
| `test_sdc.py` | SDC parser corners (generated clocks, false-paths, exclusive groups, set_input_delay) |
| `test_reporter.py` | Each output format's contract (text/JSON/SARIF) |
| `test_lint_wrapper.py` | End-to-end `lint` CLI invocation (skipped when no `yosys` on PATH) |
| `test_frontend.py` | Frontend factory dispatch + install-hint path for the slang `[slang]` extra |
| `test_slang_elaboration.py` | Rule-parity tests for the [[elaboration-frontends\|slang frontend]] on every SDC-equipped fixture |
| `test_slang_lowering.py` | Operator-coverage tests for slang lowering (binary/unary/conditional/concat/replication/`always_comb`) |
| `test_smoke.py` | `--help` and `version` CLI smoke |
| `test_rule_corners.py` | Hand-built synthetic `Module`s exercising rule-pack branches no fixture naturally hits (CDC-006 clock-port suppression, `find_crossings` hop-budget boundary) |
| `test_rules_perf.py` | Synthetic 500-flop micro-benchmark — regression sentinel for the `_RuleContext` memoisation |
| `test_gate_level_corpus.py` | Every fixture rebuilt through a Yosys gate flow; per-rule verdict must match the committed RTL netlist (see [Gate-level corpus](#gate-level-corpus)) |
| `test_flop_zoo_crossing.py` | `flop_zoo_crossing`: one `$aldff` / `$dffsr` / `$sdffce` / `$adffe` / `$dffe` capture each, the flop families no other fixture produces |
| `test_sdc_clock_variety_fixtures.py` | The SDC-variety and three-to-five-clock fixtures (see [SDC and clock-structure fixtures](#sdc-and-clock-structure-fixtures)): one verdict table checked through `analyze -f json` on both SDC backends, plus the parsed `ClockSpec` for each SDC form |

### SDC and clock-structure fixtures

Until rtl-buddy-cdc#331 nearly every fixture had two clocks, one
`set_clock_groups -asynchronous` and period-only `create_clock`. 27
fixtures now cover the rest of the SDC subset and clock structures that
need more than two domains. Each fixture's leading comment derives its
expected verdict from the rule definitions; the table in
`test_sdc_clock_variety_fixtures.py` holds it as `(rule, source
register, destination register)` triples, so a finding that moves to
another crossing fails as well as one that appears or disappears.

| Subject | Fixtures | What they pin |
|---|---|---|
| Generated clocks | `good_gen_clock_chain`, `bad_gen_clock_async_to_master`, `*_gen_clock_edges_invert`, `*_four_clock_mixed` | `-divide_by` of a `-divide_by` on pins, `-edges`, `-invert`, `-combinational`, `-multiply_by` on a port; a group naming a root covers its generated clocks; an explicit override (`-group {clk} -group {ck_div2}`) covers the named clocks only |
| Clock groups | `*_clock_groups_two_statements`, `*_four_clock_mixed`, `*_five_clock_exclusive` | two statements (a pair no single statement separates stays synchronous), several clocks per group, five groups, `-physically_exclusive` and `-logically_exclusive` beside `-asynchronous` |
| False paths | `*_false_path_one_direction`, `good_false_path_through_bus`, `bad_false_path_through_partial` | a one-way clock-pair false path covers the pair; `-through [get_pins …]` acknowledges only the bus whose paths pass the pin |
| Datapath-only | `*_datapath_only_overrides` | `-datapath_only` beats a generated relation and an exclusive group |
| Case analysis | `good_case_analysis_mode_sel0`, `bad_case_analysis_mode_sel1` | one RTL, two modes on a braced bus bit behind an inverter: the crossing disappears in one and appears in the other |
| Virtual clocks | `*_virtual_clock_io` | `set_input_delay` / `set_output_delay` against virtual clocks; port-sourced CDC-001, CDC-024 |
| Tcl | `tcl_generated_constraints` | variables, `expr`, lists, `dict`, `foreach`, `{*}` on the `tcl` backend; on `tokenizer` nothing is declared and three CDC-021 fire |
| Three to five clocks | `*_three_domain_chain`, `*_four_clock_mixed`, `*_five_clock_exclusive`, `good_reconvergence_two_source_domains`, `bad_reconvergence_three_domains`, `*_handshake_ack_via_third_domain` | relays, a crossing for every clock pair, unreachable crossings, CDC-005 only for one source resolved twice, an acknowledge relayed through a third domain |

Four strict `xfail`s record analyzer gaps these fixtures found:
`good_gen_clock_chain` (the rule context assigns domains without
`pin_clocks`, so a 2FF synchroniser clocked by a pin-declared generated
clock reads as depth 1), `*_handshake_ack_via_third_domain` (CDC-012's
feedback walk stays in the source domain), and `good_virtual_clock_io`
(CDC-006 fires on a typed port wired straight into a 2FF synchroniser).
All 27 agree across the gate-level legs.

### JSON Fixtures

Pre-built JSON fixtures are committed so the test suite **doesn't require Yosys**. Build them with:

```bash
yosys -p 'read_verilog -sv path/to/design.sv; \
          hierarchy -top <name>; proc; flatten; \
          write_json tests/fixtures/<dir>/<name>.json'
```

## Fuzz Corpus

`tests/fuzz/` renders parameterised SV + SDC from **templates**
(`tests/fuzz/templates/`), elaborates each case through Yosys (and, for
the cross-frontend oracle, the pyslang frontend), runs the analyzer and
checks the template's `expected` / `forbidden` per-rule counts. The same
cases feed four layers:

| Layer | Where | Marker |
|---|---|---|
| Per-case oracle | `test_corpus.py` | `fuzz` |
| Yosys-vs-slang differential | `test_corpus_diff.py` | `fuzz_diff` |
| Mutants (xeno SV operators + SDC operators), one parent per template family | `test_mutants.py`, `test_sdc_mutants.py` | `fuzz` |
| Per-rule firing report | `python -m tests.fuzz.coverage` | — |

The grammar fuzzer (`tests/fuzz/grammar/`, marker `fuzz_grammar`) builds
its own topologies from productions; its steering reads each
production's declared rules, so it has no per-rule table to update when
a template is added.

### One template per rule

Every rule has a template named after it (`cdcNNN.py` / `rdcNNN.py` /
`cdc_bbx.py`) that sweeps the dimensions the rule's detector compares,
not just signal names or clock periods, and pairs the firing cases with
silent and suppressed ones. The older `gap_g*.py` files are single-shape
regression sentinels from before the rules had ids (G-2 → CDC-018, G-4 →
CDC-019, G-6 → CDC-020, G-10 → CDC-021, G-7 → RDC-007, G-8 → RDC-008);
they stay, and each points at its rule's template. The first case a
template yields is a firing one, because the mutant layer mutates only
that case.

### Analyzer options

A case passes options through `params`, which the mutators copy onto
every derived case:

- `required_depth` → `--sync-depth`;
- `cdc_018_depth_threshold` → `--cdc-018-depth-threshold`;
- `sync_primitives` → `--sync-primitive`;
- `analysis="cli"` runs the case through the CLI's analysis path
  (`cli._analyze_module_and_report`) instead of calling `run_all`
  directly. Use it when a case needs that path: blackbox stubs
  (`(* blackbox *)` modules in the same source, e.g. XPM macros),
  CDC-BBX (emitted by the CLI, not the rule pack), or
  `// rbcdc: disable-rule` pragma variants. Only kept findings count.

`extra_yosys_passes` adds Yosys passes after `flatten` (no commas: CI's
Yosys splits a `-p` script at them). Templates whose subject is not
dead-cell handling add `opt_clean;`, as the `lint` script does (see
#338).

### Expected failures

The suite records two kinds of known-wrong result:

- **`gap_note`** on a case whose expectation encodes today's verdict
  although it is believed wrong. When the analyzer is fixed the case
  fails and the note explains why. Cases added for #331:
  - `cdc019_decoder_lanes_perbit_n{2,4}`: a decoder written one bit at a
    time is not seen as shared (documented scope).
  - `rdc008_primary_reset_asymmetric_chain_bus2`: one 2-bit register
    counts as one consumer.
- **`_KNOWN_SLANG_PARITY_GAPS`** in `test_corpus_diff.py` for a
  Yosys/slang disagreement. Keys are template families or single case
  ids. The list is strict both ways: an allowlisted case that agrees
  fails as well. When only some of a family's cases diverge, either key
  those cases by id or split the family by verdict (`cdc022_*` /
  `cdc022_*_ok`). The CDC-022, CDC-BBX and plain-`always` rows #331
  added were closed by #345 (pyslang blackbox support and plain `always`
  lowering), and the CDC-019 `case_n8` row, where the Yosys path was the
  one that missed, by #344. The list is empty today.

### Gate-level corpus

Committed fixture netlists are RTL-level (`$dff`, `$mux`, `$and`), so
`tests/test_gate_level_corpus.py` (rtl-buddy-cdc#331) rebuilds every
fixture that has a `.sv`, `.sdc` and committed `.json` and asserts the
analyzer reaches the same verdict at gate level. The front half
reproduces the committed build (`read_verilog -sv; hierarchy -top;
blackbox <stubs>; proc; flatten; opt_clean`, with the top and the stub
list read from the committed JSON). Three legs follow:

- **`rtl`** — nothing more. A control: it must match the committed
  verdict exactly, so a gate-leg divergence is never front-half drift
  between Yosys versions.
- **`gates`** — `techmap; opt_clean`. Pure lowering: `$_DFF_*`,
  `$_MUX_`, `$_AND_`/`$_OR_`/`$_XOR_`/`$_NOT_`, and every W-bit flop
  bit-blasted into W cells.
- **`synth`** — `opt_dff; techmap; abc -g AND,OR,XOR,MUX; opt_clean`.
  Enable and sync-reset muxes fold into `$_DFFE_*` / `$_SDFF_*` /
  `$_SDFFE_*` first, then ABC restructures the logic. Full `opt` is
  avoided because `opt_merge` collapses the duplicated synchronisers
  of the reconvergence fixtures.

"Agree" is equality of the per-rule fired-count dict, as in the
cross-frontend fuzz oracle. Counts rather than the rule-id set,
because violations are meant to be grouped per bus and per
synchroniser, so one W-bit CDC-004 turning into W CDC-001s is the
regression being looked for. Divergences live in
`_KNOWN_GATE_DIVERGENCES`, keyed by fixture and leg with a one-line
root cause, and the list is strict both ways: an unlisted divergence
fails, and a listed fixture that starts agreeing fails until its row
is removed. Comparisons are on verdicts, never netlist bytes; the
result is identical on Yosys 0.33 (the CI apt package) and 0.64.

The sweep is marked `gate_level`, skips without `yosys`, caches built
netlists under `tests/.gate_level_cache/` (keyed by Yosys version,
script and source bytes), and runs in the CI `fuzz + sim` job. Nine
fixtures also commit a `<name>.gates.json` or `<name>.synth.json`
(2FF sync, async-reset crossing, enable-flop pulse synchroniser,
enable-flop toggle without XOR tail, bus crossing, clock mux, latch,
sync-reset chain, opposite-edge chain), so the default no-Yosys job
exercises real `$_DFF_*` netlists against the same allowlist.

The same module also rebuilds every fixture with the front half's
`opt_clean` dropped and asserts the verdict is unchanged
(`test_verdict_invariant_under_opt_clean`, rtl-buddy-cdc#338): the
analyzer drops unobservable cells itself, so dead logic cannot add or
remove a finding. The three fixtures whose verdict used to change also
commit that `<name>.noclean.json` for the no-Yosys job
(`tests/test_liveness.py`).

## Simulation Oracle

`tests/sim/` (marker `sim`) simulates small DUTs under Icarus with a
behavioural metastability model (`meta_flop_lib.sv`: a flop whose `D`
changed since the previous edge may capture a random bit) and checks
that the functional failure the analyzer predicts shows up, or does
not. CI runs it in the `fuzz + sim oracle` job (apt `iverilog`); every
other job skips it.

| Test file | DUTs | Pairs with |
|---|---|---|
| `test_oracle.py` | unsynced crossing vs 2FF; latch "synchroniser" | CDC-001; gap G-1 |
| `test_glitch_oracle.py` | combinational clock mux; clock sampled as data | CDC-010; CDC-008 |
| `test_reset_oracle.py` | foreign-domain async reset vs reset sync (smoke only) | RDC-001 |
| `test_multibit_oracle.py` | binary / gray / handshake bus; reconvergent fan-out, one-hot per-bit sync, and their fixes | CDC-004; CDC-005 (+ CDC-020) |

The first three pair a DUT with a rule only in prose. The multi-bit
oracle (rtl-buddy-cdc#331) checks both halves on **one source**: each
DUT includes `meta_flop_lib.sv` for simulation, or, under
`SIM_ORACLE_SYNTH`, the plain-flop stand-ins in `meta_flop_synth.sv`
for the Yosys build (`runner.build_netlist`, a `-s` script). The
netlists are committed under `tests/sim/netlists/` (regenerate with
`uv run python -m tests.sim.runner`), so the analyzer half — exact
per-rule counts from `rtl-buddy-cdc analyze` — runs without Yosys or
Icarus; a Yosys-gated test checks each committed netlist still gives
its source's verdict. The simulation half runs eight fixed seed /
clock-ratio / phase configurations per DUT (plusargs, one compiled
image), and the test asserts the analyzer fires exactly when every
run fails, and is silent exactly when every run is clean.

**Scoreboard property** (`tb_bus_crossing.sv`): at each destination
edge, `q_out` must equal a value the source's `src_data` held at some
instant in the last `WINDOW_DST_CYCLES` (6) destination periods. A
correct crossing only delays values; an incoherent bus sample (lanes
from two source values) or a reconvergent pair whose halves come from
different source instants is a value the source never held. The
window must cover the DUT's latency; widening it can only hide errors.
A liveness count (`updates`) rejects a DUT that passes by never moving.

**Model limit.** The injection aperture is a whole destination period,
not the few picoseconds of silicon, so a lane that changed long before
the edge still counts as uncertain. The multi-bit runs therefore keep
src_clk slower than dst_clk: a gray counter advancing twice in one
destination period would show two uncertain lanes, a model artifact.

**Disagreements.** A bad DUT that never fails, or a good DUT that
fails, is a finding, not something to tune away: record it as a
strict `xfail` with a reproducer and leave `src/` alone in the oracle
change. None is open today.

## Extension Points

Where the analyzer is designed to be extended without architectural churn:

- **New rules.** Add `check_<rule>` in `rules.py` + one line in `RULES`. Reuse helpers documented in [[cdc-rule-pack]]
- **New SV attributes.** Define a frozenset + `user_<x>_flop_names(module)` helper; consult in relevant rules
- **New SDC commands.** Add handler in `sdc.py`, extend `ClockSpec` if needed
- **New output formats.** Add `render_<fmt>` in `reporter.py`, wire into `OutputFormat` and `cli._analyze_and_report`
- **New clock-network shapes.** Add cell-type set to `_BUFFER_TYPES`, `_GATE_TYPES`, or `_MUX_TYPES` in `domain.py`
- **New elaboration frontends.** Add a submodule under `frontends/`, register in the `Frontend` enum, and produce the Yosys-shape `Module` contract — see [[elaboration-frontends]]

### Not Extensible (by design)

- **Hierarchy.** Today's pipeline assumes flatten. Hierarchical analysis is a major design change, not an extension point

## Related Pages

- [[cdc-rule-pack]] — the rules being tested
- [[cdc-analysis-pipeline]] — the pipeline under test
- [[elaboration-frontends]] — the two frontends (Yosys / slang) whose parity the slang-specific test files gate
- [[rtl-buddy-cdc]] — the tool and its module map
