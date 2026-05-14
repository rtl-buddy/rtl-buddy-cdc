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

## [2026-05-14] update | slang frontend documentation catchup
- Issue: rtl-buddy/rtl-buddy-cdc#5 (Stage 2)
- New concept page `concepts/elaboration-frontends.md` — the Yosys + slang frontend layer behind `--frontend`, the `Module` contract every frontend must produce, parity status, and how to add a new frontend
- Updated `entities/rtl-buddy-cdc.md` — tags grow `slang` + `frontend`; intro says "consumes a `Module` from a Yosys or slang frontend" rather than "consumes a flattened Yosys netlist"; non-goals updated so the `lint` wrapper isn't "convenience-only" (it drives a frontend by choice); module map adds `frontend.py`, `frontends/yosys.py`, `frontends/slang.py`; exit-code-2 description generalised from "Yosys elaboration failure" to "frontend-elaboration failure"
- Updated `concepts/cdc-analysis-pipeline.md` — pipeline now starts at stage 0 (`frontend.elaborate`), then stages 1–8 are the existing pipeline; ASCII diagram updated; design-properties bullet on "no Yosys runtime dependency" generalised to "no toolchain runtime dependency on the rule pack"
- Updated `concepts/cdc-data-model.md` — note the `Module` shape is the contract every frontend produces; `Bit` integer IDs originate from either Yosys IDs or the slang frontend's sequential allocator; attribute propagation works uniformly because the slang frontend pulls via `Compilation.getAttributes(symbol)`
- Updated `index.md` — page count 9 → 10; entity summary mentions both frontends; new `[[elaboration-frontends]]` concept entry; testing-strategy summary extended with "frontends" extension point
