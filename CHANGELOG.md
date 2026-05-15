# Changelog

All notable changes to `rtl-buddy-cdc` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--strict` flag** on `analyze` and `lint` (#29). Promotes every
  `warning`-severity violation (CDC-002, CDC-005 today) to `error`
  before reporters see it, so the text banner, JSON `severity`, and
  SARIF `level` all render as `error`. Exit code is unchanged — any
  kept violation already drives exit 1; the flag is reframing, not
  gating. Suppressed findings and baseline-carried findings are left
  alone (by definition they don't drive exit-code outcomes).
- **`--baseline FILE.json` flag** on `analyze` and `lint` (#30).
  Filters out findings present in a prior JSON report (matched on
  `(rule_id, cell_name, message)`) and surfaces them as a separate
  "Carried over from baseline" tally; the carryover set never drives
  the exit code. JSON output gains `summary.baseline_carryover`
  (int) and a top-level `baseline_carryover` list; SARIF emits each
  carryover entry with a `suppressions` field tagged
  `carried over from baseline`. Baseline chains: a finding already
  in the baseline's `baseline_carryover` list stays carried over on
  the next run too, so re-baselining doesn't re-flag inherited
  findings.
- **`Frontend.auto`** + `--frontend auto` (#31). Probes
  `importlib.util.find_spec("pyslang")` at runtime and dispatches to
  `slang` when pyslang is importable, falling back to `yosys`
  otherwise. The default frontend stays `yosys` — `auto` is opt-in
  via the CLI. The `lint` preamble shows the *resolved* frontend
  (`frontend: yosys (auto)`) so log scrapers never see a third
  value.

### Changed

- JSON output now exposes a `cell_name` field on every violation
  entry. This is additive — existing `JSON_CONTRACT` keys keep their
  names and types — and gives `--baseline` a stable per-cell handle
  for the match key.

## [0.2.0] — 2026-05-15

`0.2.0` is the first release after the slang frontend reached parity
with the Yosys frontend on every SDC-equipped fixture. The headline
change makes elaboration **pluggable**: `lint --frontend slang`
elaborates SystemVerilog in-process via pyslang with no Yosys
subprocess. The rule pack, SDC parser, waiver matcher, and reporter
are unchanged — they all consume the same `netlist.Module` contract
either frontend produces.

The JSON output schema's three load-bearing keys
(`summary.violations` / `summary.suppressed` / `summary.crossings`)
are now pinned in code via `reporter.JSON_CONTRACT`, so a rename or
retype regression fails the test suite. Downstream `rtl_buddy`
keeps consuming this analyzer as a subprocess; no change to its
integration is required for the bump.

### Added

- **slang elaboration frontend** (#5, #6, #7, #8). Elaborates SV
  sources via the [pyslang](https://pypi.org/project/pyslang/)
  binding into a Yosys-shape `Module` — no synthesis subprocess, no
  `flatten` step. Covers flop inference (`$dff` / `$adff` with the
  canonical async-reset shape), combinational primitive lowering
  (`$and` / `$or` / `$xor` / `$mux` / `$not` / `$reduce_*` / …),
  `always_comb` bodies, hierarchical instance flattening with port
  aliasing, `(* attr *)` propagation, and Yosys-style `src`
  attributes (`"file:line.col-line.col"`) on every emitted cell.
  Reaches parity with the Yosys frontend on every SDC-equipped
  fixture. Opt-in via `pip install 'rtl-buddy-cdc[slang]'`; the
  default install stays `typer`-only.
- **`Frontend.elaborate` factory** (#6). New
  `rtl_buddy_cdc.frontend` module dispatches `lint --frontend
  {yosys,slang}` to the concrete frontend; the rule pack has no
  toolchain runtime dependency. `analyze` still bypasses the factory
  and takes a pre-elaborated Yosys JSON directly.
- **`reporter.JSON_CONTRACT`** (#13). Names the three load-bearing
  dotted keys at the rtl-buddy ↔ rtl-buddy-cdc subprocess boundary
  (`summary.violations` → `int`, `summary.suppressed` → `int`,
  `summary.crossings` → `int`) and pins them with new tests in
  `tests/test_reporter.py`.
- **`version` command reports pyslang version** (#25). The `version`
  subcommand always emits a `pyslang:` status line — `pyslang:
  <version>` when the optional extra is installed, `pyslang: not
  installed (optional; install with the [slang] extra)` otherwise.
  The line is unconditional so bug reports involving `--frontend
  slang` always carry the wheel version.
- **Internal-pin `create_generated_clock`** support (#1, #3).
  `ClockSpec.pin_clocks` lets `create_generated_clock -name X
  -source ... [get_pins inst/Q]` resolve at internal pins; the
  netname `/`→`.` normalisation closes the previous gap between
  Yosys-flatten output and SDC pin paths.
- **slang frontend test coverage in CI** (#9). The Test workflow
  now runs `pytest (with slang) — py3.11 / py3.12 / py3.13` against
  the matrix plus a `pytest (default install, no pyslang)` job
  that confirms the default install still works without the extra.
- **Targeted rule-pack corner coverage** (#14). New
  `tests/test_rule_corners.py` exercises CDC-006's clock-port
  suppression and `find_crossings`'s `max_hops` boundary, plus new
  cases in `tests/test_sdc.py` covering `-rise_from`/`-fall_to` and
  space-padded brace groups in `set_clock_groups`.
- **Performance sentinel test** (#12).
  `tests/test_rules_perf.py` asserts `run_all` completes in <1s
  against a synthetic 500-flop / 250-crossing module — a regression
  guard for the structural-context caching.

### Changed

- **Rule pack structural context memoised** (#12). New
  `_RuleContext` frozen dataclass holds `flops` / `domains` /
  `bit_drivers` / `reader_counts` / `d_bit_to_single_bit_flop` /
  `user_syncs` / `user_grays`; `run_all` builds one per invocation
  and threads it into every `check_cdc_NNN` via a kw-only `ctx=`
  argument. `_sync_chain_depth`'s inner chain extension collapses
  from O(N) `find_flops` scan to O(1) dict lookup. No behavioural
  change; this was the worst hot path.
- **Python version aligned on 3.13** (#10). The `.python-version`
  shipped at the repo root is `3.13` (was briefly `3.14`); the CI
  matrix covers `3.11`, `3.12`, `3.13` for the slang job.
- **`--frontend` CLI help** no longer says slang is "in development"
  (#11) — the install hint is the actual user-facing pointer now
  that the slang frontend is at fixture parity.
- **PyPI `description`** rephrased from "Yosys-backed" to "with
  pluggable Yosys or slang (pyslang) frontend" (#11).
- **pyslang pinned to a tested range** (#26). The `[slang]` extra
  declares `pyslang>=10,<11` (was `>=7.0`). 10.x is the envelope
  the slang test files were developed against; older majors had
  different `DiagnosticEngine` / `getAttributes` surfaces.

### Fixed

- **SDC parser drops every clock past the first in space-padded
  brace groups** (#23). `set_clock_groups -asynchronous -group {
  ck0 ck1 } -group { ck2 ck3 }` silently kept only `ck0` and
  `ck2` after `shlex` split the braces apart — flop-to-flop
  crossings on the dropped clocks slipped past the rule pack
  entirely. The handler now slurps until the next `-flag` and
  feeds the whole span to `_extract_clock_list`, which strips
  braces wholesale. The single-clock and no-spaces forms keep
  working.
- **slang frontend: child-instance port netnames now aliased to
  driver bits** (#15). `create_generated_clock` declarations on
  internal pins (`[get_pins u_a/clk_out_b0]`) resolved in the
  Yosys frontend but not the slang one because the child's port
  netname was carrying stale bits after a continuous-assign
  rewrite. The aliasing pass now walks `_netnames` too.

## [0.1.0] — 2026-05-11

Initial release. CDC analyzer with Yosys-flatten JSON input,
shlex-based SDC subset (`create_clock`, `create_generated_clock`,
`set_clock_groups -asynchronous` / `-logically_exclusive` /
`-physically_exclusive`, `set_false_path -from/-to`,
`set_input_delay -clock`, `set_output_delay -clock`), CDC-001
through CDC-008 rule pack, Spyglass-`.swl`-style waiver matcher,
and text / JSON / SARIF reporters.

[Unreleased]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rtl-buddy/rtl-buddy-cdc/releases/tag/v0.1.0
