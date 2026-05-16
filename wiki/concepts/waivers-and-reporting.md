---
title: Waivers and Reporting
created: 2026-05-11
updated: 2026-05-14
type: concept
tags: [waivers, reporting, cli, sarif, testing]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# Waivers and Reporting

## Waivers

`waivers.py` implements a minimal waiver-file format. One statement per line:

```
waive <RULE-ID|*> <regex> [reason ...]
```

### Matching Order

The matcher tries the regex against three strings per violation, most specific → least:

1. The violation's `cell_name`
2. The canonical `"src_flop -> dst_flop"` text (when there's a crossing)
3. The violation's `message`

A hit on any of the three suppresses. First matching waiver wins (top-down).

### Behavior

- Suppressed findings are **kept in the report** (with matching reason and waiver line number), not silently dropped
- They appear in JSON output and in SARIF as `suppressions`
- They **don't drive the exit code** — a fully-waived run returns 0
- Deliberately mimics Spyglass `.swl` workflow at a smaller surface: no scope qualifiers, severity overrides, or expiry dates

## Reporting

Three formatters share the same `AnalysisResult` input. Format selection is purely a CLI flag (`--format text|json|sarif`); the analyzer pipeline runs the same regardless.

### `render_text`
Human-readable. Module summary, domain counts, crossing list, then violations grouped by rule. Inside each rule group, findings are bucketed by `Violation.instance_path` under per-instance headers (`[top]` for top-level, `u_block_a / u_sync` for nested paths); the bucketing collapses to a flat layout when every finding in the group is at top, so output on flat IP-block fixtures is byte-identical to pre-hierarchical-reporting. Designed for terminal and CI-log review.

### `render_json`
Full structured output with a stable schema. Used by `rb cdc` to extract violation counts and by custom dashboards. The downstream contract: `summary.violations` (int), `summary.suppressed` (int), `summary.crossings` (int). These three keys are pinned in code by `reporter.JSON_CONTRACT` and a paired test that fails on rename or retype — see `tests/test_reporter.py::test_json_contract_keys_present_and_typed`.

Every violation / suppressed / baseline-carryover entry also carries `instance_path: list[str]` (always present, `[]` at top, never `null` or missing), and a top-level `by_instance` list aggregates *kept* violations by path with per-rule counts. The `--baseline` match key (`rule_id`, `cell_name`, `message`) is intentionally not extended with `instance_path` — historical baselines do not re-flag after the field was added.

### `render_sarif`
SARIF 2.1.0, GitHub Code Scanning compatible. Populates `tool.driver.rules` for every rule that fired. Each result carries `physicalLocation.region` parsed from `cell.attributes["src"]`, and — when `instance_path` is non-empty — a `logicalLocations` entry with `fullyQualifiedName` (dot-joined path) and `kind: "module"`. Suppressed findings emit with a `suppressions` field so the alert exists but doesn't fail the build. Output is validated against the OASIS-published SARIF 2.1.0 schema by `tests/test_sarif_schema.py` across five render paths.

## The Reporter Contract

`AnalysisResult` is the immutable struct at the boundary between analyzer and presentation. It contains: `module`, `domains`, `crossings` (all), `async_crossings` (post-SDC filter), `spec`, `violations` (kept), `suppressed`. **No formatter does additional analysis.**

## Related Pages

- [[cdc-data-model]] — `AnalysisResult` and `Violation` dataclasses
- [[cdc-rule-pack]] — rules that produce the violations
- [[rtl-buddy-cdc]] — CLI flags and exit codes
