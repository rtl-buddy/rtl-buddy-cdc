---
title: CDC Rule Pack
created: 2026-05-11
updated: 2026-05-11
type: concept
tags: [cdc, synchronization, crossing, clock-domain, algorithm, extension-point]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# CDC Rule Pack

`rules.py` is a flat collection of `check_<rule>` functions covering CDC-001 through CDC-008, plus a small registry and shared structural helpers.

## Registry

```python
RULES: dict[str, RuleFn] = {
    "CDC-001": check_cdc_001,
    "CDC-002": check_cdc_002,
    ...
    "CDC-008": check_cdc_008,
}

def run_all(module, crossings, clock_spec, required_depth=2):
    out = []
    for rule_id, rule in RULES.items():
        if rule_id == "CDC-002":
            out.extend(check_cdc_002(module, crossings, clock_spec, required_depth))
        else:
            out.extend(rule(module, crossings, clock_spec))
    return out
```

CDC-002 is the only rule that takes a configuration parameter (`required_depth`, exposed via `--sync-depth`). Adding a new rule is a one-line edit in `RULES` plus the function definition.

## Shared Helpers

Several rules depend on the same structural detectors, all free functions in `rules.py`:

| Helper | Used by | Purpose |
|---|---|---|
| `_sync_chain_depth` | CDC-001, -002, -003 | Walk forward from flop Q lane-wise; count chain length of single-reader same-domain flops |
| `_is_multibit_sync_first_stage` | CDC-004 | Verify destination is width-N flop whose Q equals another same-domain flop's D lane-wise |
| `_is_gray_encoded_source` | CDC-004 | Backward-walk from src D for canonical `g = b ^ (b >> 1)` XOR pattern |
| `_is_gated_bus_crossing` | CDC-004 | Recognise handshake gating — D updates only when enable from dst domain is synchronized |
| `_clock_network_cells` | CDC-008 | Identify cells driving any flop CLK; exempted from "clock as data" |
| `user_sync_flop_names` | CDC-001..-003, -006 | Flops annotated `(* cdc_sync *)` |
| `user_gray_flop_names` | CDC-004 | Source-side flops annotated `(* cdc_gray *)` |

## Severity Policy

| Severity | Meaning | Drives exit code? |
|---|---|---|
| `error` | High-confidence CDC bug — structural shape is unambiguous | Yes (1 if any kept) |
| `warning` | Might be intentional or a bug; review and fix or waive | Yes (1 if any kept) |
| `info` | Reserved for future structural-fact reports | No |

## Recognition vs. Annotation

Each rule can exempt a crossing via **structural recognition** (netlist shape analysis) or **user annotation** (SV `(* attr *)` tags). Both paths are valid. The structural detector is primary — the attribute is the relief valve, not the front door. Annotation is preferred when:

- The structural detector can't see the shape after flatten (e.g. vendor sync-cell macro as black-box)
- The chain uses a non-canonical implementation the detector won't accept
- The user wants to mark the cell as reviewed

## Adding a Rule

1. Write `check_cdc_NNN(module, crossings, clock_spec) -> list[Violation]` in `rules.py`
2. Reuse existing helpers where applicable
3. Register in `RULES`; special-case in `run_all` if it needs config
4. Pick severity per the policy table
5. Add paired bad + good fixtures

## Related Pages

- [[cdc-data-model]] — `Violation` dataclass produced by rules
- [[crossing-detection]] — `Crossing` records consumed by rules
- [[waivers-and-reporting]] — how violations are partitioned and reported
- [[clock-domain-tracing]] — CDC-008's dependency on `_clock_network_cells`
