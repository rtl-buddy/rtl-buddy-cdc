---
title: CDC Testing Strategy
created: 2026-05-11
updated: 2026-11-13
type: concept
tags: [testing, cdc, architecture, extension-point, frontend, fuzz]
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
  `cdc022_*_ok`).

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
script and source bytes), and runs in the CI `fuzz + sim` job. Eight
fixtures also commit a `<name>.gates.json` or `<name>.synth.json`
(2FF sync, async-reset crossing, enable-flop pulse synchroniser, bus
crossing, clock mux, latch, sync-reset chain, opposite-edge chain), so
the default no-Yosys job exercises real `$_DFF_*` netlists against
the same allowlist.

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
