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

## [2026-05-14] update | slang frontend polish: concat / replication / source locations
- PR: rtl-buddy/rtl-buddy-cdc#8 (stacked on rtl-buddy/rtl-buddy-cdc#7)
- Updated `concepts/elaboration-frontends.md` — RHS lowering bullets now list `ConcatenationExpression` + `ReplicationExpression` (pure LSB-first bit-tuple aliasing, no cell emitted); new "Source Locations" subsection covering `_src_attr`'s priority chain (`node.syntax.sourceRange` → `node.sourceRange` → degenerate point from `node.location`) and the Yosys `"file:line.col-line.col"` output convention
- Updated `raw/articles/rtl-buddy-cdc-architecture.md` §3.1 — Frontend Layer paragraph expanded to enumerate the expression-lowering coverage (binary / unary / conditional / select / concat / replication) and the `src` attribute the frontend now attaches to every emitted cell
- Updated `README.md` Roadmap — "slang frontend" Implemented entry mentions concatenation / replication and the Yosys-style `src` source-location attributes surfaced via JSON / SARIF reporters

## [2026-05-14] update | post-slang-frontend docs catchup
- Issue: rtl-buddy/rtl-buddy-cdc#11
- `concepts/cdc-testing-strategy.md` — removed stale "Not Extensible: Only Yosys `write_json` is supported" claim (contradicted `concepts/elaboration-frontends.md` since the frontend abstraction landed in #6); added "New elaboration frontends" to the Extension Points list pointing at the frontend page; refreshed Test Files table to enumerate `test_handshake_fixture`, `test_clock_gating`, `test_marked_user_sync`, `test_lint_wrapper`, `test_frontend`, `test_slang_elaboration`, `test_slang_lowering`, `test_smoke` (was missing 7 of 12 modules)
- `concepts/cdc-rule-pack.md` — shared-helpers table reorganised into three categories (Index helpers / Structural walks / Attribute lookups); added previously-undocumented `_q_to_flop`, `_bit_drivers`, `_bit_reader_count`, `_backward_fanin`, `_backward_flop_fanin`; new "Rule-internal grouping logic" subsection describing CDC-005's `(src_flop, dst_clock)` grouping and CDC-007's `(src_flop, src_clk, dst_clk)` reset-tree grouping + the rule-internal `_async()` closure
- `README.md` — "MVP usable" framing → "Usable on IP-block-sized designs" with explicit mention of two frontends at parity; intro line names the slang frontend; refreshed Architecture ASCII diagram to show the `frontend.elaborate` factory with both backends (yosys + slang); new SARIF local-inspection pointer (VS Code SARIF Viewer + Microsoft's web component) at the end of the Output formats section
- `pyproject.toml` — `description` rephrased from "Yosys-backed" to "with pluggable Yosys or slang (pyslang) frontend" (visible PyPI listing copy)
- `src/rtl_buddy_cdc/cli.py` — `--frontend` Typer help no longer says slang is "in development — see issue #5"; replaced with the actual install hint (`pip install 'rtl-buddy-cdc[slang]'`)

## [2026-05-14] update | rule-pack structural memoisation (#12)
- New `_RuleContext` frozen dataclass + `_build_context(module, clock_spec)` builder in `rules.py` holding `flops` / `domains` / `bit_drivers` / `reader_counts` / `d_bit_to_single_bit_flop` / `user_syncs` / `user_grays`. `run_all` builds one per invocation and threads it into every `check_cdc_NNN` via a keyword-only `ctx=` argument; rules called standalone lazy-build their own.
- `_sync_chain_depth` now takes the `d_bit_to_single_bit_flop` reverse-index from the ctx, turning chain extension from an O(N) `find_flops` scan into an O(1) dict lookup. This was the worst hot path — called per crossing × per chain step.
- Removed two now-dead module-level helpers `_q_to_flop` / `_bit_drivers` (their work moved into `_build_context`); dropped unused `q_to_flop` parameter from `_sync_chain_depth`'s signature; `RuleFn` type alias loosened to `Callable[..., list[Violation]]` so the kw-only ctx is expressible.
- Updated `concepts/cdc-rule-pack.md` — "Index helpers" table reframed around `_RuleContext` fields (since they're no longer free functions); new "Rule context" subsection explaining lazy-build + the perf motivation; cross-link to the new sentinel test.
- New `tests/test_rules_perf.py` — synthetic 500-flop / 250-crossing module asserts `run_all` completes in <1s. Future regression sentinel for the structural-context caching.
