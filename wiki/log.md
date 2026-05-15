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

## [2026-05-14] update | JSON output schema contract pinned in code (#13)
- New `reporter.JSON_CONTRACT` mapping (`summary.violations` / `summary.suppressed` / `summary.crossings` → `int`). Names the three load-bearing keys the rtl-buddy ↔ rtl-buddy-cdc subprocess boundary depends on — previously documented only in prose in AGENTS.md.
- New tests in `tests/test_reporter.py`: `test_json_contract_keys_present_and_typed` (every contract key resolves to the declared type), `test_json_contract_includes_waived_run` (a waivered run exercises both `summary.violations` and `summary.suppressed` in the same render, catching value-swap regressions the first test alone would miss), `test_sarif_suppression_shape` (SARIF `suppressions: [{kind:external, status:accepted, justification:…}]` payload).
- `concepts/waivers-and-reporting.md` — `render_json` section now cross-references the new constant + test.
- `AGENTS.md` § "Cross-repo coupling" — JSON-schema bullet now points at the constant + test so a contributor changing the reporter knows where the contract is encoded.

## [2026-05-14] update | targeted coverage for rule-pack corners (#14)
- New `tests/test_waivers.py::test_cli_waiver_drops_exit_code_json` + `..._sarif` — end-to-end CLI plumbing for waivered runs in JSON and SARIF formats. The pre-existing text-format test left both dispatches unverified; a swap or drop of the `suppressed[]` / `suppressions` payload would have slipped through.
- New `tests/test_rule_corners.py` (file added) — hand-built synthetic `Module`s exercising:
  - **CDC-006's clock-port suppression** (rules.py `if p in clock_ports: continue`): a clock signal reaches a synchronizer's D pin through a `$buf`; CDC-008 fires, CDC-006 must stay quiet. Verified before commit that the test goes red if the suppression branch is bypassed (manually re-ran with an empty `ClockSpec` and observed a CDC-006 false-positive).
  - **`find_crossings` hop budget**: three boundary cases — 5-buffer chain hidden at default `max_hops=4`, revealed at `max_hops=5`, exact-match at `max_hops=4` with a 4-buffer chain (inclusive boundary).
- New `tests/test_sdc.py::test_false_path_rise_from_fall_to_recognized` — `_ENDPOINT_FLAGS` covers `-rise_from` / `-fall_from` / `-rise_to` / `-fall_to` but no test exercised the edge-qualified variants.
- New `tests/test_sdc.py::test_set_clock_groups_brace_with_spaces_inside` — pins the brace-split re-glob fallback in `_handle_set_clock_groups` (the `_extract_clock_list(args[i+1] + " " + args[i+2])` path) for the single-clock case it currently handles. The fallback's multi-clock case has a known limitation (only the first clock survives) documented in the test docstring — separate concern.
- `concepts/cdc-testing-strategy.md` — Test Files table extended with `test_rule_corners.py` and `test_rules_perf.py` (the latter was added in #12 but missed in the table refresh).

## [2026-05-14] update | fix SDC parser dropping clocks in space-padded brace groups (#23)
- Bug surfaced in #14: `_handle_set_clock_groups` silently dropped every clock past the first when a `-group { ck0 ck1 }` clause had whitespace inside the braces. shlex splits that to five tokens (`{`, `ck0`, `ck1`, `}`) and the old re-glob-the-next-token fallback only recovered `ck0`. The downstream effect was silent — `are_async(ck1, ck2)` returned False instead of True, so flop-to-flop crossings on those clocks slipped past the rule pack entirely.
- Fix: swap the re-glob fallback for a slurp-until-next-flag loop matching the pattern `_handle_set_false_path` already uses. After `-group`, walk forward until the next `-flag` and feed the whole span to `_extract_clock_list`, which strips braces wholesale. The single-clock case (`-group { ck0 }`) still works, the multi-clock case (`-group { ck0 ck1 }`) now works, and the no-spaces form (`-group {ck0 ck1}`) continues to work.
- Tests in `test_sdc.py`: renamed the existing #14 test to `..._single_clock`; added `..._multi_clock` (the issue reproducer verbatim — four cross-group async pairs all True) and `..._no_spaces_multi_clock` (regression sentinel for the case that worked before).

## [2026-05-15] update | add mypy CI job + clean up domain.py type:ignore comments (#28)
- New `mypy` job in `.github/workflows/lint.yml` alongside `ruff-check`. Lax baseline (`uv run mypy` with `[tool.mypy]` config in `pyproject.toml`) — Python 3.11 target, src/ scope only, `pyslang.*` excluded via `follow_imports = "skip"` because the pyslang stubs (auto-generated `.pyi` from the C++ binding) have a SyntaxError mypy can't parse.
- `src/rtl_buddy_cdc/domain.py` — replaced two `dict[str, object]` accumulator dicts (`grouped`, `port_grouped`) with `TypedDict`s (`_CrossingGroup`, `_PortCrossingGroup`). Stored already-narrowed `src_clock`/`dst_clock` as `str` rather than carrying `FlopDomain` references with their nullable `clock` field, so the `Crossing(...)` construction site needs no further narrowing. Removed all 14 `# type: ignore[union-attr | index | operator | arg-type]` comments that had been masking the mypy/pyright disagreement on the previous `object`-typed values. Renamed the port-grouped loop's `g` to `pg` so mypy doesn't carry narrowing from the first loop's `_CrossingGroup` into the second loop's `_PortCrossingGroup`.
- `src/rtl_buddy_cdc/cli.py` — added explicit `list[Violation]` / `list[SuppressedViolation]` annotations to `_analyze_module_and_report`'s `violations` / `suppressed` locals (previously `= []` inferred as `list[Any]`).
- `src/rtl_buddy_cdc/reporter.py` — `_violation_to_dict`'s `out` widened from inferred `dict[str, str]` to `dict[str, object]` so the nested-dict assignments for `crossing` and `location` type-check cleanly.
- Sanity check verified by intentionally reverting the `_CrossingGroup` annotation back to `dict[str, object]` — mypy immediately reports 8 errors at the `Crossing(...)` site (`arg-type` on every `g[...]` access), confirming the new job's drift-sentinel role works.
- `AGENTS.md` — Validation commands grew `uv run mypy`; CI bullet now mentions ruff + mypy as the lint jobs.
