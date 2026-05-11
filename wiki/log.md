# Wiki Log

> Chronological record of all wiki actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, update, query, lint, create, archive, delete
> When this file exceeds 500 entries, rotate: rename to log-YYYY.md, start fresh.

## [2026-05-11] create | Wiki initialized
- Domain: EDA tooling and CDC analysis — rtl-buddy-cdc architecture, algorithms, data models, and design decisions
- Structure created with SCHEMA.md, index.md, log.md

## [2026-05-11] ingest | rtl-buddy-cdc Architecture (docs/architecture.md)
- Raw source: `raw/articles/rtl-buddy-cdc-architecture.md`
- Created entity page: `entities/rtl-buddy-cdc.md`
- Created concept pages:
  - `concepts/cdc-analysis-pipeline.md`
  - `concepts/cdc-data-model.md`
  - `concepts/clock-domain-tracing.md`
  - `concepts/crossing-detection.md`
  - `concepts/sdc-parsing.md`
  - `concepts/cdc-rule-pack.md`
  - `concepts/waivers-and-reporting.md`
  - `concepts/cdc-testing-strategy.md`
- All pages cross-referenced with [[wikilinks]] (minimum 2 outbound links each)
- index.md populated with all 9 pages

## [2026-05-11] update | Internal-pin `create_generated_clock` support
- Issue: rtl-buddy/rtl-buddy-cdc#1
- Updated `concepts/clock-domain-tracing.md` — new "Internal-Pin Generated Clocks" section covering `bit_to_clock`, `_build_bit_to_clock`, and the `/`→`.` netname normalisation
- Updated `concepts/sdc-parsing.md` — `pin_clocks` behavior + shlex-tolerant `-source` parsing
- Updated `concepts/cdc-data-model.md` — `ClockSpec.pin_clocks` field
- Updated `raw/articles/rtl-buddy-cdc-architecture.md` — new §5.1, expanded §6.1, `ClockSpec` field list
