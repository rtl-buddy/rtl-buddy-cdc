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
