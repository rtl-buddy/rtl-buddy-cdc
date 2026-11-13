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
  - `cdc018_chain_depth_d6_tapport2`: a chain stage read by a top-level
    output port still counts as a chain stage, so CDC-018 fires.
  - `cdc019_decoder_lanes_case_n8`: Yosys lowers the 8-way case decoder
    to a ROM, and CDC-019 misses it (also on the differential allowlist).
  - `cdc019_decoder_lanes_perbit_n{2,4}`: a decoder written one bit at a
    time is not seen as shared (documented scope).
  - `rdc008_primary_reset_asymmetric_chain_bus2`: one 2-bit register
    counts as one consumer.
  - `cdc_bbx_blackbox_boundary_clock_output`: CDC-008 also reports the
    stub's own clock output pin.
  - `cdc_bbx_blackbox_boundary_multiclk_wr_rd_registered`: a primitive
    registered with `--sync-primitive` whose clock pins carry no
    `src` / `dest` hint is still reported as CDC-BBX.
- **`_KNOWN_SLANG_PARITY_GAPS`** in `test_corpus_diff.py` for a
  Yosys/slang disagreement. Keys are template families or single case
  ids. The list is strict both ways: an allowlisted case that agrees
  fails as well. When only some of a family's cases diverge, either key
  those cases by id or split the family by verdict (`cdc022_*` /
  `cdc022_*_ok`). The CDC-022, CDC-BBX and plain-`always` rows #331
  added were closed by #345 (pyslang blackbox support and plain `always`
  lowering); the CDC-019 `case_n8` row, where the Yosys path is the one
  that misses, remains.

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
