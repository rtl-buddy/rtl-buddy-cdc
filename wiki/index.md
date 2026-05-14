# Wiki Index

> Content catalog. Every wiki page listed under its type with a one-line summary.
> Read this first to find relevant pages for any query.
> Last updated: 2026-05-14 | Total pages: 10

## Entities
- [[rtl-buddy-cdc]] — Python-based CDC linter consuming a `Module` from a Yosys or slang frontend; goals, non-goals, module map, and rtl-buddy integration

## Concepts
- [[cdc-analysis-pipeline]] — The pure-function pipeline from elaborated `Module` to text/JSON/SARIF report
- [[cdc-data-model]] — All immutable dataclasses: Module, Flop, FlopDomain, Crossing, ClockSpec, Violation, AnalysisResult
- [[cdc-rule-pack]] — CDC-001..-008 rule functions, shared structural helpers, severity policy, and recognition vs. annotation
- [[cdc-testing-strategy]] — Paired bad/good fixtures, test organization, and extension points for rules/attributes/SDC/formats/frontends
- [[clock-domain-tracing]] — `trace_clock_root` algorithm: buffer/ICG/mux/divider cell categories, walk mechanics, CDC-008 role
- [[crossing-detection]] — BFS-based `find_crossings`: walk, grouping by (src,dst), intentional exclusions, complexity
- [[elaboration-frontends]] — Yosys + slang frontends behind `--frontend`; shared `Module` contract; how to add a new frontend
- [[sdc-parsing]] — `shlex`-based SDC parser design, supported commands, deliberate omissions, diagnostics policy
- [[waivers-and-reporting]] — Waiver file format and matching, text/JSON/SARIF formatters, AnalysisResult contract

## Comparisons

## Queries
