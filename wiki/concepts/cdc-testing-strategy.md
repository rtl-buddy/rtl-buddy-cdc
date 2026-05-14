---
title: CDC Testing Strategy
created: 2026-05-11
updated: 2026-05-14
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

### JSON Fixtures

Pre-built JSON fixtures are committed so the test suite **doesn't require Yosys**. Build them with:

```bash
yosys -p 'read_verilog -sv path/to/design.sv; \
          hierarchy -top <name>; proc; flatten; \
          write_json tests/fixtures/<dir>/<name>.json'
```

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
