---
title: CDC Testing Strategy
created: 2026-05-11
updated: 2026-11-13
type: concept
tags: [testing, cdc, architecture, extension-point, frontend]
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
