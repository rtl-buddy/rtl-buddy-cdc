---
title: CDC Analysis Pipeline
created: 2026-05-11
updated: 2026-05-14
type: concept
tags: [pipeline, cdc, algorithm, architecture, data-model, frontend]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# CDC Analysis Pipeline

The end-to-end flow from SV sources (or a pre-elaborated Yosys netlist) plus an SDC to a violation report. Orchestrated by `cli._analyze_module_and_report`, the pipeline is a sequence of pure-function stages.

## Pipeline Stages

```
SV sources + --top          pre-elaborated
      │                       netlist.json
      ▼                            │
(0) frontend.elaborate              │
   ├─ yosys (default)               │
   └─ slang (pyslang)               │
      ▼                              ▼
                Module
                  ▼
(1) flops.find_flops → (2) domain.assign_domains → (3) domain.find_crossings
    → (4) sdc.parse_file → (5) cli._filter_async → (6) rules.run_all
    → (7) waivers.apply → (8) reporter.render_*
```

The frontend stage (0) produces a `Module` from either a frontend invocation (the `lint` CLI command path) or a pre-existing Yosys JSON via `netlist.load` (the `analyze` CLI command path). After that point the pipeline is identical regardless of source.

**Steps 0–3 are deterministic and SDC-independent** — they always run and populate the structural summary. Steps 4–8 only fire if an SDC was supplied; without one the tool prints the structural summary and exits 0.

### Stage Details

0. **Elaboration** ([[elaboration-frontends|frontend layer]]): `lint --frontend {yosys,slang}` drives the selected frontend, which produces a `Module` that satisfies the Yosys-shape contract. `analyze --netlist file.json` bypasses this stage and loads the JSON via `netlist.load` directly.
1. **Flop recognition** (`flops.find_flops`): Scans cells against the 11-entry `FF_CELL_TYPES` zoo, extracting CLK / D / Q pins.
2. **Domain assignment** (`domain.assign_domains`): Calls `trace_clock_root` for each flop's CLK net to determine which top-level clock port drives it.
3. **Crossing detection** (`domain.find_crossings`): BFS from each flop's Q pins forward through combinational cells until hitting destination flop D pins in different clock domains.
4. **SDC parsing** (`sdc.parse_file`): Parses the CDC-relevant SDC subset into a `ClockSpec`.
5. **Async filtering** (`cli._filter_async`): Drops crossing pairs not declared async in the SDC. Consults `is_unreachable_crossing` (exclusive groups) before `are_async`.
6. **Rule application** (`rules.run_all`): Runs CDC-001 through CDC-008 against the filtered crossings.
7. **Waiver application** (`waivers.apply`): Partitions violations into kept vs. suppressed based on the waiver file.
8. **Reporting** (`reporter.render_*`): Formats the `AnalysisResult` as text, JSON, or SARIF.

## Design Properties

- **Pure functions.** Side effects belong only in `cli.py` (file I/O), the frontends (subprocess / pyslang invocation), and the reporter (writing to a file-like). `domain.py`, `rules.py`, and `sdc.py` have no I/O.
- **Immutable dataclasses** by default. Frozen everywhere except `ClockSpec`'s parser-built collections.
- **No toolchain runtime dependency on the rule pack.** The `analyze` entry consumes a pre-elaborated `Module` directly. The `lint` entry drives a [[elaboration-frontends|frontend]], but which one is the user's choice — `typer` is the only mandatory runtime dependency; the `[slang]` extra adds pyslang opt-in.

## Related Pages

- [[rtl-buddy-cdc]] — the tool this pipeline powers
- [[elaboration-frontends]] — stage 0: how a `Module` gets built from SV sources
- [[cdc-data-model]] — the dataclasses that flow through the pipeline
- [[clock-domain-tracing]] — stage 2: how flops get assigned to clock domains
- [[crossing-detection]] — stage 3: BFS-based crossing discovery
- [[sdc-parsing]] — stage 4: SDC constraint parsing
- [[cdc-rule-pack]] — stage 6: the CDC-001..-008 rule functions
