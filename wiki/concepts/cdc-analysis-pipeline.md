---
title: CDC Analysis Pipeline
created: 2026-05-11
updated: 2026-05-11
type: concept
tags: [pipeline, cdc, algorithm, architecture, data-model]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# CDC Analysis Pipeline

The end-to-end flow that turns a Yosys netlist + SDC into a violation report. Orchestrated by `cli._analyze_and_report`, the pipeline is a sequence of pure-function stages.

## Pipeline Stages

```
netlist.json → (1) netlist.load → (2) flops.find_flops → (3) domain.assign_domains
    → (4) domain.find_crossings → (5) sdc.parse_file → (6) cli._filter_async
    → (7) rules.run_all → (8) waivers.apply → (9) reporter.render_*
```

**Steps 1–4 are deterministic and SDC-independent** — they always run and populate the structural summary. Steps 5–9 only fire if an SDC was supplied; without one the tool prints the structural summary and exits 0.

### Stage Details

1. **Netlist loading** (`netlist.load`): Parses Yosys `write_json` output into typed `Module` / `Cell` / `Port` / `Netname` structs.
2. **Flop recognition** (`flops.find_flops`): Scans cells against the 11-entry `FF_CELL_TYPES` zoo, extracting CLK / D / Q pins.
3. **Domain assignment** (`domain.assign_domains`): Calls `trace_clock_root` for each flop's CLK net to determine which top-level clock port drives it.
4. **Crossing detection** (`domain.find_crossings`): BFS from each flop's Q pins forward through combinational cells until hitting destination flop D pins in different clock domains.
5. **SDC parsing** (`sdc.parse_file`): Parses the CDC-relevant SDC subset into a `ClockSpec`.
6. **Async filtering** (`cli._filter_async`): Drops crossing pairs not declared async in the SDC. Consults `is_unreachable_crossing` (exclusive groups) before `are_async`.
7. **Rule application** (`rules.run_all`): Runs CDC-001 through CDC-008 against the filtered crossings.
8. **Waiver application** (`waivers.apply`): Partitions violations into kept vs. suppressed based on the waiver file.
9. **Reporting** (`reporter.render_*`): Formats the `AnalysisResult` as text, JSON, or SARIF.

## Design Properties

- **Pure functions.** Side effects belong only in `cli.py` (file I/O) and the reporter (writing to a file-like). `domain.py`, `rules.py`, and `sdc.py` have no I/O.
- **Immutable dataclasses** by default. Frozen everywhere except `ClockSpec`'s parser-built collections.
- **No Yosys runtime dependency** on the primary `analyze` path. The standalone `lint` wrapper shells out to Yosys, but the Python core does not.

## Related Pages

- [[rtl-buddy-cdc]] — the tool this pipeline powers
- [[cdc-data-model]] — the dataclasses that flow through the pipeline
- [[clock-domain-tracing]] — stage 3: how flops get assigned to clock domains
- [[crossing-detection]] — stage 4: BFS-based crossing discovery
- [[sdc-parsing]] — stage 5: SDC constraint parsing
- [[cdc-rule-pack]] — stage 7: the CDC-001..-008 rule functions
