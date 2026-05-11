---
title: CDC Testing Strategy
created: 2026-05-11
updated: 2026-05-11
type: concept
tags: [testing, cdc, architecture, extension-point]
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
| `test_waivers.py` | Waiver parsing and matching |
| `test_sdc.py` | SDC parser corners |
| `test_reporter.py` | Each output format's contract |

### JSON Fixtures

Pre-built JSON fixtures are committed so the test suite **doesn't require Yosys**. Build them with:

```bash
yosys -p 'read_verilog -sv path/to/design.sv; \
          hierarchy -top <name>; proc; flatten; \
          write_json tests/fixtures/<dir>/<name>.json'
```

## Extension Points

Where the analyzer is designed to be extended without architectural churn:

- **New rules.** Add `check_<rule>` in `rules.py` + one line in `RULES`. Reuse helpers from §8.1
- **New SV attributes.** Define a frozenset + `user_<x>_flop_names(module)` helper; consult in relevant rules
- **New SDC commands.** Add handler in `sdc.py`, extend `ClockSpec` if needed
- **New output formats.** Add `render_<fmt>` in `reporter.py`, wire into `OutputFormat` and `cli._analyze_and_report`
- **New clock-network shapes.** Add cell-type set to `_BUFFER_TYPES`, `_GATE_TYPES`, or `_MUX_TYPES` in `domain.py`

### Not Extensible (by design)

- **Netlist front-end.** Only Yosys `write_json` is supported. A different front-end needs a `Module`-shaped adapter, not an abstraction
- **Hierarchy.** Today's pipeline assumes flatten. Hierarchical analysis is a major design change, not an extension point

## Related Pages

- [[cdc-rule-pack]] — the rules being tested
- [[cdc-analysis-pipeline]] — the pipeline under test
- [[rtl-buddy-cdc]] — the tool and its module map
