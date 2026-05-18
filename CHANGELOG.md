# Changelog

All notable changes to `rtl-buddy-cdc` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **RDC-005 — Multiple reset sources converging without muxing**
  (fourth and final sub-PR of #114, seventh instalment of #107).
  New rule: a flop's async reset pin is the output of comb logic
  whose backward fanin reaches two or more distinct top-level
  reset ports, with no ``$mux``/``$pmux`` selecting which source is
  active. Both resets are simultaneously active and the user has
  no control over which dominates. Complementary to RDC-004:
  fires precisely on the comb-of-ports case RDC-004 deliberately
  skips, so every comb-on-reset shape ends up owned by exactly one
  of the two rules. Severity ``warning`` — the AND-of-resets
  pattern is common enough in SoC designs that ``error`` would be
  too strong; the rule invites review. Suppressed when the
  immediate driver cell is ``$mux``/``$pmux`` (explicit-muxing
  exemption) or when the consumer is recognised as a
  reset-synchroniser chain member. Paired fixtures
  ``bad_rdc_005_multi_source_reset`` /
  ``good_rdc_005_muxed_reset``. Closes #114.
- **RDC-004 — Reset driven by combinational logic** (third sub-PR of
  #114, sixth instalment of #107). New rule: a flop's async reset
  pin is the output of a combinational gate (``$and``/``$or``/
  ``$mux``/etc.) whose backward fanin reaches one or more flops.
  Comb outputs can glitch when inputs transition asynchronously,
  producing spurious reset assertions. Fires on ``$adff*`` consumers
  only (sync resets filter sub-cycle glitches at the clock edge);
  pure comb-of-ports (e.g. ``rst_a_n & test_mode_n``) is accepted
  as the user's responsibility and doesn't fire — keeps the noise
  floor low on designs that legitimately AND two external reset
  ports. Severity ``error``. Suppressed when the consumer is
  recognised as a reset-synchroniser chain member. Paired fixtures
  ``bad_rdc_004_comb_driven_reset`` /
  ``good_rdc_004_registered_reset`` (the good case demonstrates
  the textbook fix: register the comb output on the local clock
  before using as a reset).
- **RDC-003 — Sync reset crossing** (second sub-PR of #114, fifth
  instalment of #107). New rule: a flop's synchronous reset pin
  (``SRST``) is driven — directly or through combinational logic —
  by a flop in a different asynchronous clock domain. The sync
  reset is sampled on the destination clock's rising edge and the
  cross-domain source can be metastable on the sample cycle. Detection
  is the SRST analogue of RDC-001's ARST walk; findings are grouped
  by ``(src_flop, src_clk, dst_clk)`` so one foreign-domain source
  feeding many sync-reset consumers becomes one finding (mirrors
  RDC-001's reset-tree grouping). Severity ``error``. Paired
  fixtures ``bad_rdc_003_sync_reset_crossing`` /
  ``good_rdc_003_sync_reset_synced`` (the good case demonstrates the
  textbook fix: a 2FF reset synchroniser in the destination clock
  domain between the foreign source and the consuming ``$sdff``).

### Changed

- **RDC-002 narrowed to async-reset consumers.** The polarity-
  mismatch check now skips ``$sdff*`` consumers — sync-reset signals
  are intentional gating (e.g. a "kill" signal that synchronously
  clears a pipeline), not part of the async-reset distribution tree
  where the "consumer must enter reset when producer does"
  invariant holds. Caught while bringing up the RDC-003 fixture
  pair: both fixtures triggered RDC-002 as a false positive on the
  ``$sdff`` consumer. RDC-003 owns the sync-reset crossing concern.

### Added

- **RDC-002 — Reset polarity mismatch** (first sub-PR of #114, fourth
  instalment of #107). New rule: a flop's reset pin is driven
  *directly* (no inverter, no comb between) by another flop's ``Q``,
  and the consumer's ``ARST_POLARITY`` doesn't match the producer's
  ``ARST_VALUE`` — so the consumer never enters reset when the
  producer does (a polarity wiring bug). Severity ``error``.
  Consumes the foundation pass from #110 and the reset-synchroniser
  recogniser from #112 — flops the recogniser identifies as a sync
  stage are skipped (the user may have built an intentional
  polarity-inverting sync on purpose; the recogniser's constant-fed-
  head check is what distinguishes that from accidental wiring).
  Findings are grouped by ``(producer, polarities)`` so a single
  upstream wiring bug feeding N consumers becomes one report listing
  every affected destination — matches RDC-001's reset-tree grouping
  convention and the fix-shape the user has to take. Paired fixtures
  ``bad_rdc_002_polarity_mismatch`` / ``good_rdc_002_polarity_match``.
  A pre-existing polarity asymmetry in ``bad_reset_tree`` (source
  flop resets to ``1'b1``, consumers expect active-low) is now caught
  too — the slang test that pinned the rule list updated to
  ``["RDC-001", "RDC-002"]``.

### Changed

- **Rule `CDC-007` renamed to `RDC-001`** (#113, third instalment of
  the RDC family from #107). Internal-only rename — the reset-tree
  grouping behaviour is unchanged, and no cross-repo consumer
  pattern-matches on the literal rule_id string (verified against
  `rtl_buddy` and `rtl-buddy-project-template`). Existing waiver
  files written against `CDC-007` continue to suppress the renamed
  rule via a legacy-id alias map in
  `rtl_buddy_cdc.waivers._LEGACY_RULE_ALIASES`. The reporter's
  short-form description on `RDC-001` carries a "(was CDC-007)"
  parenthetical so log readers see the breadcrumb. Tests, README
  rule table, and the slang frontend's fixture-id docstring all
  rename to `RDC-001`; the new alias path is pinned by
  `test_waivers.py::test_legacy_cdc_007_alias_suppresses_rdc_001`.

### Added

- **Reset-synchronizer recognizer** (second slice of #107).
  `rtl_buddy_cdc.reset_domain.find_reset_synchronizers(module,
  clock_domains, *, min_depth=2)` returns the set of flop cell names
  participating in a textbook async-assert / sync-deassert reset
  chain: ≥`min_depth` same-clock flops sharing the same async-reset
  source, chained Q→D, whose head flop's D pin is a constant. The
  load-bearing distinction vs. the naive "same ARST + same clock"
  walk is that data-path register chains which *happen* to share an
  upstream reset are correctly rejected — the head's D must be a
  constant. No rule consumes this yet; subsequent RDC rules (polarity
  mismatch, comb-driven reset) will skip recognised synchronizers to
  avoid false positives on legitimate ones.
- **`reset_domain` analysis module — foundation pass** (first slice of
  #107). New `src/rtl_buddy_cdc/reset_domain.py` exposing
  `ResetSource`, `ResetDomain`, and `assign_reset_domains(module)` —
  the structural facts the upcoming RDC (Reset Domain Crossing) rule
  family will share, parallel to how `assign_domains` underpins the
  clock-domain rule pack. Per-flop output classifies each flop's
  reset pin (or absence of one) by reset type (sync/async), polarity
  (high/low, decoded from Yosys' `*_POLARITY` parameter), and source
  kind (port / inferred-from-flop / constant / comb). No rule pack
  consumes this yet — the existing CDC-007 walk is unchanged. Rule
  rerooting and the RDC-001..-005 family land in follow-up PRs so
  each rule's firing shape and any contract changes (rule_id alias,
  message text) can be reviewed independently.
- **`--emit-domain-map` structured clock-domain artifact** (#106).
  New CLI option on `analyze` and `lint` that writes a stable v1.0
  JSON sidecar (`schema_version: "1.0"`) capturing the analyzer's
  clock-domain view independently of the findings stream: clocks +
  generated clocks + clock groups + false-path pairs, per-flop
  domain assignments with source locations, the typed port→clock
  map, and structural crossings tagged with `async_per_sdc`. Pair
  with the new `--no-findings` flag to skip rule evaluation
  entirely — useful when the artifact is the sole deliverable
  (downstream consumers like [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view)
  don't need the rule findings). All collections are sorted by a
  documented key so a golden-file diff against the same inputs
  stays empty. Endpoint identifiers (`flop_domains[].instance_path`,
  `crossings[].src_flop`/`dst_flop`) use the same `<top>.<parents>.<leaf>`
  dotted form so consumers can join them. Implementation in the
  new `rtl_buddy_cdc.domain_map` module; contract pinned by
  `tests/test_domain_map.py` (12 checks including a golden-file diff
  against the `ip_cdc_handshake` fixture).
- **CDC-009 — Pulse-width / fast-to-slow data-loss** (#47 design,
  #101 detection, #102 fixtures, #103 integration). Catches the
  textbook fast-to-slow case where a single-cycle src-domain pulse may
  land entirely between two slower dst-clock rising edges and be lost
  (data loss without metastability ever entering the picture). Fires
  on flop-sourced, single-bit, async crossings when the SDC declares
  both clock periods, ``src_period * 1.5 < dst_period``, and the src
  flop's ``D`` pin matches the edge-detector pattern ``A & ~A_d``
  (with ``A_d`` the 1-cycle delay of ``A``). Severity ``warning`` —
  single-bit pulse loss is a methodology smell, pairs cleanly with
  CDC-001/002 (missing sync). Detection lives in
  ``rtl_buddy_cdc.pulse.classify_d_pin_shape``; the rule is
  deliberately false-negative-biased so handshake / pulse-stretcher /
  toggle-sync idioms naturally fall outside the matched pattern and
  stay silent without an explicit waiver. Three paired fixtures:
  ``bad_pulse_width_fast_to_slow`` (must fire), and good counterparts
  ``good_pulse_width_stretched`` (counter-based pulse stretcher) and
  ``good_pulse_width_handshake`` (req/ack handshake with synced-back
  ack). The implementation deviates from the issue body's §2 in one
  detail — see ``pulse.py``'s module docstring; the corrected pattern
  is the textbook one from Cummings SNUG 2008 §4.5.
- **CDC-011 — Unconstrained primary input captured by clocked logic**
  (#97). Surfaces the SDC-discipline gap where a top-level input port
  has no `set_input_delay -clock <name>` typing but physically reaches
  a flop's `D` pin. Implementation seam: `sdc.UNCONSTRAINED_SENTINEL`
  + `sdc.synthesize_unconstrained_inputs(spec, module)` assigns the
  sentinel to every untyped input port after parse; the existing
  port-walk in `find_crossings` then emits port-sourced crossings the
  rule can consume. `check_cdc_011` consolidates by source port:
  single destination domain → `warning` (typical fix is SDC typing);
  two or more distinct destination domains → `error` (a single port
  cannot be synchronous to multiple clocks). CDC-001 / CDC-002 /
  CDC-006 each gain a sentinel skip so they don't double-fire with
  fix-advice mismatch. Parser now also surfaces a `partial_warnings`
  entry when `set_*_delay` names ports but omits `-clock` (previously
  silent). Four paired fixtures land: `bad_unconstrained_input_two_domains`
  (multi-domain scalar — error), `bad_unconstrained_input_derived_clock`
  (AND-of-clocks — warning), `bad_unconstrained_input_bus_two_domains`
  (multi-domain bus — error), `bad_unconstrained_input_muxed_clock`
  (test-mode mux — warning), each with a `good_*_typed` counterpart.
  Ten existing `bad_*` fixtures retro-typed (`set_input_delay -clock
  src_clk` on their data inputs) so each fires only the rule it was
  authored to test; `ip_cdc_handshake` and `good_2ff_sync` updated
  the same way.
- **Design proposal: CDC-010, glitches on the clock network from a
  wrong-domain control signal** (#48). New
  `docs/proposals/clock-network-glitch.md` covers the failure mode
  (a clock mux's select or an ICG's enable driven by a flop in a
  foreign domain chops the output clock), the detection shape
  reusing `_clock_network_cells` and `_backward_flop_fanin`, why
  this is the complement of CDC-008 rather than a duplicate, the
  paired-fixture sketch (`bad_async_clock_mux/` /
  `good_sync_clock_mux/`), and the open questions blocking a tech-
  mapped second pass. Severity is `error`; suggested rule ID
  `CDC-010` (the next free slot after the upcoming CDC-009 from
  #47). Implementation tracked by follow-up issues — this entry is
  documentation only; no rule behavior changes in this PR.
- **Hierarchical reporting** (#46). Every violation gains an
  `instance_path: tuple[str, ...]` field resolved at the
  `cli._analyze_and_report` boundary from the cell's name. The
  resolver normalises both the Yosys-flatten shape
  (`$flatten\u_a.\u_b.<leaf>` — one `$flatten\` prefix regardless of
  depth) and the slang frontend's dotted shape (`u_b0.q`) into the
  same `tuple[str, ...]`, with the top instance never appearing as
  a component. The rule pack stays frontend-agnostic — no `reporter`
  import in `rules.py`. Rendering changes are additive: text reporter
  buckets findings under per-instance headers inside each rule group
  (`[top]` / `u_block_a / u_sync`), collapsing back to the flat
  layout when every finding lives at top; JSON output gains
  `instance_path: list[str]` on every violation entry plus a
  top-level `by_instance` aggregation of kept violations; SARIF
  results gain a `logicalLocations` entry alongside the existing
  `physicalLocation` when the path is non-empty. `JSON_CONTRACT` and
  the `--baseline` match key are untouched — historical baselines
  do not re-flag on first run after the bump. Phased landing in
  #83 / #87 / #88 / #89; the resolver's nested-flatten handling
  was corrected in #90 after a template-design demo surfaced that
  Yosys emits exactly one `$flatten\` prefix per cell (not one per
  level). Design captured in
  `wiki/raw/articles/hierarchical-reporting.md` (#82).
- **SARIF 2.1.0 schema validation in the test suite** (#80). The
  OASIS errata01 SARIF schema is vendored at
  `tests/schemas/sarif-2.1.0.json` (CI does not reach out to
  schemastore.org or docs.oasis-open.org); `tests/test_sarif_schema.py`
  validates `render_sarif` output across four render paths
  (clean, with-violations, waiver-suppressed, baseline-carryover)
  plus rule-shape variants. Caught schema drift the shape-assertion
  tests miss — a typo'd field name, a wrong-type value, a missing
  required sub-field on a code path no shape assertion happens to
  read. `jsonschema` added to the `test` dep group; runtime deps
  stay typer-only.
- **slang frontend: `$dff` / `$adff` WIDTH and ARST_VALUE
  parameters** (#40). Emitted flops carry the parameters Yosys
  populates for the same shapes, so downstream consumers that key
  off `cell.parameters["WIDTH"]` (or read the reset polarity from
  `ARST_VALUE`) see consistent values across frontends.
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

### Internal

- **CI: pytest coverage measurement** (#120). The `pytest (with
  slang)` job in `test.yml` now runs with `--cov --cov-report=term-
  missing --cov-fail-under=78`; `[tool.coverage.run]` is configured
  for the `src/rtl_buddy_cdc` package with branch coverage on. The
  78% floor sits a few points below the observed 81% baseline so
  small refactors don't trip the gate while still failing CI on a
  meaningful regression. The threshold is passed on the CLI (not
  in `[tool.coverage.report]`) so the no-slang env can run its own
  pytest without inheriting it. The `pytest-no-slang` companion
  job stays plain `pytest -q` by design (its purpose is the
  install-hint path, not coverage).

### Changed

- JSON output now exposes a `cell_name` field on every violation
  entry. This is additive — existing `JSON_CONTRACT` keys keep their
  names and types — and gives `--baseline` a stable per-cell handle
  for the match key.
- **CDC-005 false-positive cut** (#32, #33). The rule used to fire
  whenever a single source flop drove ≥2 sync chains in the same
  destination domain — purely structural. It now also requires the
  chains to reconverge downstream: phase-2 walks each chain's
  terminal flop's Q forward through the netlist (helper:
  `_forward_reachable_cells`) and only fires when ≥2 chains'
  forward cones touch a common downstream cell (flop OR comb,
  including a comb cell driving an unregistered output port).
  Existing `bad_reconvergent_sync` and new
  `bad_reconvergent_with_recombine` fixtures still fire; the new
  `good_disjoint_fanout_sync_chains` fixture (single source, two
  sync chains, disjoint downstream registers) is correctly silent.
  Phase 1 added the `_forward_reachable_flops` helper alongside;
  it's the strict-flop variant retained for callers that need
  flop-only reachability.
- **CDC-004 gating-shape widening** (#34, #35).
  `_is_gated_bus_crossing` used to recognise exactly one shape: a
  `$mux` directly driving every bit of the destination flop's `D`
  with a dst-domain select. Two more shapes are accepted now:
  - **`$dffe`-style EN gating** (#34). When the destination cell is
    a flop-with-enable from `flops.FF_CELL_TYPES` (`$dffe` /
    `$sdffe` / `$adffe` / …) and its `EN` pin's fanin is entirely
    in the destination clock domain, the crossing is gated by a
    synchronized load-enable and CDC-004 stays silent. The default
    Yosys and slang frontend pipelines emit `$dff` + `$mux` (not
    `$dffe`), so the new path activates only on externally-supplied
    netlists or with `opt_dff` in the build. Paired fixture
    `good_dffe_gated_bus_crossing` (built with `opt_dff`) is silent.
  - **Buffered mux→D** (#35). Up to two transparent single-input
    buffers (`$buf` / `$pos` / `$_BUF_` / `$_NOT_` / `$not`) between
    the gating mux's `Y` output and the destination flop's `D` pin
    are tolerated — Yosys synthesis routinely inserts a fanout
    buffer or two here. Chains deeper than the budget bail out and
    keep firing. Paired fixture `good_buffered_gated_bus_crossing`
    (one `$_BUF_` per lane, post-processed onto the Yosys output by
    `insert_buffer.py`) is silent; the 3-hop regression case in
    `tests/test_rule_corners.py` still fires CDC-004.

  Both extensions are pure widenings of the gated-crossing
  acceptance set — `bad_bus_crossing` (no gating at all) and
  `ip_cdc_handshake` (direct mux-on-D, no buffers) keep their
  pre-existing behaviour.

### Fixed

- **slang frontend: emit `$dlatch` for `always_latch` blocks** (#39).
  The frontend used to silently drop every ``always_latch`` block —
  no cell emitted, so legitimate latches (the ICG enable-latch in
  the ``clock_gating`` fixture and any other glitch-free integrated
  clock gate) had no driver in the resulting netlist. The
  procedural-block dispatch now recognises ``AlwaysLatch`` and lowers
  the canonical single-arm ``if (en) lhs = rhs;`` shape to a
  ``$dlatch`` with ``EN`` / ``D`` / ``Q`` connections and the
  ``WIDTH`` / ``EN_POLARITY`` parameters Yosys writes for the same
  shape. Inverted-enable conditions (``if (~clk)``) lower the
  inversion through a ``$not`` cell driving ``EN`` rather than
  folding into ``EN_POLARITY=0``, matching the slang frontend's
  existing convention for conditional polarity. The latch type
  intentionally stays outside ``flops.FF_CELL_TYPES`` — latches
  don't bound clock domains and stay transparent to
  ``find_crossings`` by design (out-of-scope per #39). Multi-arm /
  case / explicit-else latch bodies and ``$adlatch`` (async-reset
  latch) remain unmodelled and fall through silently, matching
  Yosys's ``proc_dlatch`` envelope.
- **slang frontend: `case` statement completeness** (#84, #85).
  Two paired holes in the `CaseStatement` lowering, both visible only
  on parameter-driven or FSM-style RTL the procedural walker reaches
  but previously mishandled:
  - **Compile-time-constant case-expr folding** (#84). `case (MODE)`
    with a parameter-bound `MODE` used to walk every arm, so dead
    arms' RHS bits leaked into the deferred-emission mux tree and the
    rule pack reported false-positive crossings through statically-
    unreachable case items. Folds via `_const_int` (mirroring #72's
    if/else fold) and walks only the live arm — or `defaultCase` when
    no explicit match — so the resulting netlist matches what Yosys-
    flatten + `opt_clean` would prune.
  - **Per-arm enable inference for dynamic case-expr** (#85). Each
    arm's body used to walk with no enable pushed, so writes
    collapsed to a last-write-wins (or unconditional) value at drain
    time and the gated-bus detector couldn't see arm-gated loads —
    the standard FSM "load bus inside a case arm" shape. Each item's
    enable is now the `$eq` of the case-expr with its match (chained
    `$or` for multi-match items such as `2'd0, 2'd1:`); the default
    arm's enable is `$not` of the OR of explicit-match equalities.
    Pushed onto `_enable_stack` so the deferred-emission drain builds
    a `$mux` tree gated by the arm-selection bits. Bails to walking
    every arm unconditionally when the case-expr can't be lowered
    (same conservative shape as the pre-PR behaviour, so any design
    that previously hit the walker still does).
  Casez / casex / `inside`-set match remain out of scope and bail via
  `_bits_of_expression` returning None, same as today.
- **slang frontend: sync-reset shape emits `$sdff`** (#86). After
  PR #74's sync-reset shape detection landed, sync-reset bodies —
  ``always_ff @(posedge clk) if (!rst_n) <constants> else <data>`` —
  walked only the data arm but still produced ``$adff`` cells wired
  to the reset signal, with ``ARST_POLARITY`` defaulted to active-
  high (the event list never carried an async-reset event to read
  the polarity off). Two bugs in one cell: wrong cell type relative
  to Yosys-flatten (which materialises ``$sdff`` here), and wrong
  polarity even within the ``$adff`` shape. ``_classify_reset_check``
  now returns ``(symbol, kind, active_low)`` and threads ``kind``
  through ``_drain_proc_writes`` → ``_emit_flop_cell``, which
  branches on it to emit ``$sdff`` with ``SRST`` / ``SRST_POLARITY``
  / ``SRST_VALUE`` for the sync case. Sync polarity is derived from
  the condition shape itself (``!rst_n`` / ``~rst_n`` → active-low,
  bare ``rst`` → active-high) since the event list has nothing to
  read there. Async and no-reset paths are unchanged; the rule pack
  is cell-type-tolerant via ``flops.FF_CELL_TYPES`` so existing
  detection isn't affected.
- **slang frontend: `always_comb if/else` both-arms emission** (#64).
  An if/else where both arms wrote the same LHS dropped one arm
  because the per-LHS `$mux` emission keyed on the canonical
  variable and overwrote the alias before the second arm walked.
  Fix defers the emission until both branches have been visited.
- **slang frontend: nested element-select / 2-D port alias** (#69).
  A 2-D port driven from an indexed select (`x[i][j]`) didn't
  resolve through the alias chain because the resolver stopped at
  the first element-select. Walk now recurses through nested
  selects.
- **resolver: Yosys multi-level flatten** (#90). The phase-1
  resolver assumed Yosys emits `$flatten\u_a.$flatten\u_b.<leaf>`
  for nested flatten. Real Yosys emits exactly *one* `$flatten\`
  prefix per cell regardless of nesting depth, with deeper
  hierarchy encoded as additional dot-separated `\`-escaped
  instance identifiers. Symptom on the project-template
  `demo_tiny_alu_subsys` design: findings inside
  `u_hs_cmd/u_sync_ack` and `u_hs_result/u_sync_ack` bucketed under
  just `u_hs_cmd` / `u_hs_result`, dropping the inner level. Fix
  walks dot-separated tokens after the prefix until one starts with
  `$` (that's the leaf), accumulating instance components (with the
  Yosys identifier-escape `\` stripped) in between.

### Internal

- `_RuleContext` gains a `bit_consumers` index (the forward analog
  of `bit_drivers`), built once per `run_all`. Used by the
  CDC-005 reconvergence filter and available to any future rule
  that needs to walk consumer fanout. New helper `_sync_chain_flops`
  exposes the chain as an ordered flop tuple so callers that need
  the chain's tail (not just its depth) don't duplicate the walk
  inside `_sync_chain_depth`.
- New helper `_trace_through_bus_buffers` walks each destination
  `D` bit backward through a bounded chain of transparent single-
  input buffer cells (`_BUS_BUFFER_TYPES`) and returns the driver
  of the surviving upstream net. Currently consumed by CDC-004's
  buffered mux-on-D detection; available to future rules that need
  to look past Yosys-inserted fanout buffers without owning a
  buffer-walker each.

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
