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

## [2026-05-11] update | docs/source consistency audit
- README.md / AGENTS.md — retarget `docs/architecture.md` link to `wiki/raw/articles/rtl-buddy-cdc-architecture.md` (the doc moved during the wiki ingest)
- README.md — fix log-level claim: unknown SDC commands are dropped at DEBUG, not INFO (matches `sdc.py:221`)
- `entities/rtl-buddy-cdc.md`, `raw/articles/rtl-buddy-cdc-architecture.md` — `__init__.py` is not empty; it exposes a `main()` shim to `cli.app`
- `concepts/clock-domain-tracing.md`, `raw/articles/rtl-buddy-cdc-architecture.md` §5.1 — correct the claim that CDC-008 "uses the same walker"; `_clock_network_cells` is a separate reverse-BFS in `rules.py` that mirrors the same cell-type taxonomy but doesn't share code with `trace_clock_root`
- `concepts/cdc-data-model.md` — annotate that `Crossing.src_clock` may be a generated clock name (not only a top-level port) since the pin_clocks work
- `concepts/cdc-testing-strategy.md` — replace dangling `§8.1` ref with `[[cdc-rule-pack]]` wikilink

## [2026-05-11] update | source-sync internal fixtures + trace signature
- New paired fixtures in `tests/fixtures/{good,bad}_source_sync_internal/` exercise internal-pin `create_generated_clock` end-to-end; covered by `tests/test_bad_source_sync_internal.py` and a new entry in `test_good_fixtures.py`
- `trace_clock_root` signature gained `bit_to_clock: dict[Bit, str] | None = None`; `assign_domains` and `find_crossings` gained `pin_clocks: dict[str, str] | None = None`
- `sdc.py` `_handle_create_generated_clock` now consumes `-source [get_*]` by scanning forward to the next `-` flag (fixes shlex-split leak of `]`-suffixed name into the trailing target list)
