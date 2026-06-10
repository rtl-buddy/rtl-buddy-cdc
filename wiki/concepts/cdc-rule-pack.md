---
title: CDC Rule Pack
created: 2026-05-11
updated: 2026-05-14
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

Several rules depend on the same structural detectors, all free functions in `rules.py`. The index helpers are precomputed once per `run_all` invocation by `_build_context()` and threaded into each rule via the keyword-only `ctx=` parameter — see [Rule context](#rule-context) below.

### Index helpers (precomputed in `_RuleContext`)

| Field on `_RuleContext` | Used by | Purpose |
|---|---|---|
| `flops` | CDC-006, -007, -008 | Frozen tuple of every `Flop` in the module |
| `domains` | CDC-001..-007 | `cell_name → clock-port` mapping (the rule-pack view of `assign_domains`) |
| `bit_drivers` | CDC-003, -004, -006, -007, -008 | `bit → (cell_name, output_port, bit_idx)` map of every driver |
| `reader_counts` | CDC-001, -003, -005, -006 (via `_sync_chain_depth`) | Per-bit reader count — anchors the "exactly one reader" predicate |
| `d_bit_to_single_bit_flop` | CDC-001, -002, -003, -005, -006 (via `_sync_chain_depth`) | `bit → Flop` reverse index for single-bit-D flops, turning `_sync_chain_depth`'s chain-extension step into an O(1) lookup |
| `user_syncs` | CDC-001, -002, -003, -006 | Cell names of flops annotated `(* cdc_sync *)` |
| `user_grays` | CDC-004 | Cell names of source-side flops annotated `(* cdc_gray *)` |
| `user_handshakes` | CDC-001, -013, -014, -020 | Cell names of flops annotated `(* cdc_handshake *)` — participants in a sanctioned four-phase req/ack vector-CDC primitive (issue #247) |

### Structural walks

| Helper | Used by | Purpose |
|---|---|---|
| `_sync_chain_depth` | CDC-001, -002, -003, -006 | Walk forward from flop Q lane-wise; count chain length of single-reader same-domain flops |
| `_backward_fanin` | CDC-006 | Reverse-BFS through comb cells; returns the set of flop `Q`s and top-level input ports reached |
| `_backward_flop_fanin` | CDC-004 (mux select), CDC-007 (ARST) | Reverse-BFS through comb cells; returns only flop `Q`s reached (ignores port endpoints) |
| `_is_multibit_sync_first_stage` | CDC-004 | Verify destination is width-N flop whose Q equals another same-domain flop's D lane-wise |
| `_is_gray_encoded_source` | CDC-004 | Backward-walk from src D for canonical `g = b ^ (b >> 1)` XOR pattern |
| `_is_gated_bus_crossing` | CDC-004 | Recognise handshake gating — D updates only when enable from dst domain is synchronized |
| `_clock_network_cells` | CDC-008 | Identify cells driving any flop CLK; exempted from "clock as data" |

### Attribute lookups (called by `_build_context`)

| Helper | Used by | Purpose |
|---|---|---|
| `user_sync_flop_names` | populates `ctx.user_syncs` | Flops annotated `(* cdc_sync *)` / `(* synchronizer *)` / `(* async_reg *)` |
| `user_gray_flop_names` | populates `ctx.user_grays` | Source-side flops annotated `(* cdc_gray *)` / `(* gray_code *)` |
| `user_handshake_flop_names` | populates `ctx.user_handshakes` | Flops annotated `(* cdc_handshake *)` / `(* req_ack_handshake *)` — suppress CDC-001/-013/-014/-020 on the crossing keyed at the tagged flop |

## Rule context

`_RuleContext` is a frozen dataclass holding the precomputed views above. `run_all` builds one per invocation and passes it to every `check_cdc_NNN` as a keyword-only argument; standalone test invocations of an individual rule lazy-build their own context when `ctx=None`. The motivation is that prior to this caching the rule pack rebuilt `assign_domains` 7× per `run_all`, and `_sync_chain_depth`'s inner loop re-scanned every flop per chain-extension step — invisible on IP-block fixtures, dominant on anything larger. See `tests/test_rules_perf.py` for the synthetic-500-flop regression sentinel.

### Rule-internal grouping logic

- **CDC-005** groups single-bit, depth≥2 crossings by `(src_flop_name, dst_clock)` and fires only when one source flop drives ≥2 sync chains.
- **CDC-007** collects ARST → foreign-domain-flop edges grouped by `(src_flop_name, src_clk, dst_clk)` so one async reset source feeding many destinations becomes a single "reset distribution tree" violation listing every destination. The `_async(a, b)` closure inside the rule defers to `clock_spec.are_async()` when an SDC is present and falls back to "distinct domains are async" otherwise.

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
