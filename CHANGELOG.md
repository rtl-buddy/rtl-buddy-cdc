# Changelog

All notable changes to `rtl-buddy-cdc` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--project-root` anchors relative path args** (#245). The
  path-bearing args `--yosys-plugin`, `--emit-domain-map`, and
  `--emit-reset-domain-map` are now resolved relative to a stable base
  rather than the process cwd: `--project-root` if given, else the
  directory of `--sdc`, else cwd (the legacy behaviour). This kills the
  off-by-N-levels breakage a driver hit when it forwarded relative paths
  verbatim while running the tool from a deeply-nested artefact dir.
  Absolute paths are unaffected. Two companion fixes ship with it: the
  `--emit-*` targets now `mkdir -p` their parent before writing (so
  emitting into an uncommitted dir like `.rtl-buddy/overlays/` on a fresh
  checkout no longer raises `FileNotFoundError`), and a
  `--yosys-plugin` not-found error now reports the resolved absolute
  path so it's actionable.

- **CDC-021: flop CLK driven by undeclared port** (#206). Closes
  G-10 from the #188 coverage survey. Companion to CDC-011 on the
  clock-pin side: fires when a flop's `CLK` traces back to a
  top-level input port that has no `create_clock` declaration. The
  failure is silent — undeclared port-clocks don't appear in any
  async group, so `_filter_async` drops every crossing involving
  the undeclared domain and every other rule that touches it stays
  silent. CDC-021 surfaces the methodology bug so the user can
  declare the clock and let the other rules do their work.

- **CDC-019: independently-synced one-hot decode across CDC**
  (#204). Closes G-4 from the #188 coverage survey. Fires when
  N≥2 single-bit source-domain flops sharing a common
  combinational driver (one-hot decoder, priority arbiter, case-
  statement output, etc.) each have an async crossing to flops in
  the same destination clock domain. CDC-004 misses this shape
  because each registering flop is structurally 1-bit; CDC-019
  groups by `(driver_comb_cell, dst_clock)` so the related lanes
  are reported as one finding listing every affected lane.
  Suppressed via `(* cdc_gray *)` / `(* cdc_static *)` /
  `(* cdc_sync *)` on any source flop.

- **RDC-007: reset-sync chain accepted with deassertion-polarity
  wired backwards** (#202). Closes G-7 from the #188 coverage
  survey. The structural recogniser
  (`find_reset_synchronizers`) accepts any constant-fed head
  regardless of which constant; RDC-007 cross-checks the head's
  D constant against the chain's reset polarity (active-low → D
  must be `1'b1`; active-high → `1'b0`). A chain loading the
  asserted value instead is a one-shot that never deasserts —
  worse, the structural recogniser silently exempts every
  downstream consumer from RDC-001..-006. New
  `iter_reset_sync_chains` helper enumerates each recognised
  chain once with its head D constant attached;
  `find_reset_synchronizers`'s public contract is unchanged.

- **`(* glitchless_clock_mux *)` SV attribute for CDC-010
  suppression** (#208). Closes G-9 from the #188 coverage survey.
  Attach to a clock-mux select wire to vouch that the surrounding
  mux topology is glitch-free (a cross-coupled-latch envelope or
  a foundry library cell that handles the safe handoff). CDC-010's
  standard "synchronise the select" fix advice would actually
  break a correctly-built glitchless mux by introducing a single-
  clock dependency that defeats the other-clock-aware gating; the
  attribute is the user's explicit promise, parallel to
  `(* cdc_sync *)` and `(* cdc_gray *)`. CDC-010 stays silent on
  any control-pin bit it walks that belongs to a tagged netname.

- **CDC-020: sliced-bus reconvergence across CDC** (#210). Closes
  G-6 from the #188 coverage survey. Fires when a genuinely-multi-
  bit source flop (WIDTH≥2) has its bits sliced into N≥2 width=1
  crossings that each independently cross to flops in the same
  destination clock domain. CDC-004 misses this shape because each
  per-lane crossing's width is 1; the multi-bit-bus detector's
  `width <= 1` skip drops every lane even though the source bus is
  genuinely multi-bit. Sibling of CDC-019 (shared comb decoder).
  Same suppression as CDC-004: `(* cdc_gray *)` / `(* cdc_static *)`
  on the source, plus the structural gray-encode-into-multi-bit-
  sync exemption.

- **CDC-018: cascaded synchroniser warning** (#212). Closes G-2
  from the #188 coverage survey. Quality-of-life check that fires
  when a CDC crossing's destination sync chain depth reaches
  `--cdc-018-depth-threshold` (default 4). Classic pattern: two
  engineers each added their own 2FF sync on the same wire, or a
  refactor left the original chain in place when a new wrapper was
  added — depth-4+ chains that still work but add latency without
  improving MTBF. Severity `warning`. Suppressed by `(* cdc_sync *)`
  on the chain head; threshold configurable for high-MTBF designs
  via the new CLI flag.

- **G-5 handshake reporter refinement** (#214). Closes G-5 from
  the #188 coverage survey. CDC-001 / CDC-002 findings whose
  async domain pair matches a CDC-012 finding now carry a one-line
  `[handshake-related]` tag pointing at the CDC-012 partner. The
  two rule families catch different views of the same incomplete-
  handshake protocol — CDC-012 sees "gated bus with no synced-back
  ack", CDC-001 / CDC-002 sees "src→dst single-bit crossing lacks a
  2FF sync chain" — and a user looking at one shouldn't have to
  mentally correlate the other. Pure reporter refinement (no new
  rule, no firing-set changes); implemented as a final
  post-processing pass in `run_all`.

- **RDC-008: unsynced primary-reset-port deassertion** (#216).
  Closes G-8 from the #188 coverage survey. Fires when a flop's
  async reset is driven directly by a top-level input port and the
  flop is not part of a recognised reset-synchroniser chain. RDC-001
  is the symmetric rule for foreign-domain flop-sourced resets;
  RDC-008 fills the port-source gap. **Asymmetric-intent detection**:
  only fires when the user has built a sync chain for the port in
  *some* clock domain but missed it in another (and the missing-chain
  domain has ≥2 unsynced consumers) — narrows the rule to the
  methodology bug while staying silent on designs that use the raw
  port directly everywhere (a common simplification in small RTL).

- **SDC clock-graph conflict linter** (#218). Closes G-11 from the
  #188 coverage survey. New `validate_clock_graph(spec)` in
  `rtl_buddy_cdc.sdc` runs after `parse_file` and emits cross-
  statement diagnostics that the per-command parsers don't see:
  same top-level port claimed by multiple clocks; generated clock
  with an unresolved `-master_clock`; generated-clock master
  cycles (A→B→A). Duplicate clock names (two
  `create_clock -name X`) are caught inline by the per-command
  handlers. All diagnostics flow through the existing
  `spec.partial_warnings` surface so they reach the user via the
  text-format warning channel; JSON/SARIF promotion to proper
  `Violation` records is a deferred followup.

- **Per-fixture `README.md` with mermaid clock-domain diagrams**
  (#164). Every fixture under `tests/fixtures/` now has a generated
  README that browsers see when navigating the directory on GitHub:
  prose pulled from the leading `//` block of the primary `.sv`
  file, a facts line (status / top / clocks / crossings), and the
  `## Clock-domain map` rendered via `rtl-buddy-cdc render`. The
  one hand-written README (`ip_cdc_handshake/`) is preserved by
  detecting the absence of the generator tag. Regenerate with
  `uv run python scripts/gen_fixture_docs.py`; `--check` mode is in
  place for a future CI drift sentinel.

- **Domain-map schema 1.1: `clock_network_crossings[]`** (#168). New
  top-level list capturing flop→flop relationships that travel via
  the clock network — a foreign-domain flop driving a clock-mux
  select or ICG enable whose output reaches another flop's CLK pin.
  Same hazard CDC-010 already detects, exposed as a structural
  parallel to `crossings[]` so consumers can render the edge that
  the data-fanout walker can't see. Each entry carries
  `src_flop` / `dst_flop` (hier paths), `src_clock` / `dst_clock`,
  the gating cell triple (`control_cell`, `control_cell_type`,
  `control_pin`), and `control_kind` ∈ {`mux-select`,
  `gate-enable`}. `async_per_sdc` is always `true` — the walker
  only emits pairs where the source domain is async to *every*
  clock-input domain of the controlled cell, mirroring CDC-010's
  firing condition. Lives in a new module
  `src/rtl_buddy_cdc/clock_network.py` (function
  `find_clock_network_crossings`) so it stays separate from the
  rule pack's `Violation` surface while reusing the same
  structural helpers via import from `rules.py`. Schema bump is
  additive — v1.0 consumers ignore the field, and the renderer
  treats absence as empty. The mermaid renderer in
  `rtl_buddy_cdc.render` draws each entry with a thick arrow and
  a `⚡ clk-ctrl (mux S)` / `(gate EN)` label so the CDC-010
  hazard is visible at a glance.

### Fixed

- **CDC-012: feedback-presence check scoped per crossing, not per
  domain pair** (#239). The "is a synced-back handshake present?"
  predicate was cached on the `(src_clock, dst_clock)` domain pair,
  so the first crossing with dst→src feedback short-circuited
  *every* gated multi-bit crossing between the same clocks. A module
  wiring two independent crossings — one proper req/ack handshake,
  one broken req-only — would see the handshake's ack feedback and
  silence the broken crossing. `_has_dst_to_src_feedback` now takes
  the `Crossing` and walks only its source flop's register-
  neighbourhood (the payload register's `D`/`EN` fanin, hopping flop
  → input fanin → flop within the src domain) for a path back to a
  dst-domain flop; the cache is keyed on `c.src_flop.cell.name`.
  Surfaced by the #238 grammar gap-mining run (170/1000 seeds
  predicted CDC-012 but it didn't fire; now 0). Paired fixtures
  `bad_mixed_handshake_datahold` / `good_mixed_handshake_datahold`
  pin both directions.

- **Domain-map: flop and crossing clock names canonicalise through
  the SDC port→clock table** (#166). Previously, when
  `trace_clock_root` walked through a clock mux and stopped at a
  literal port name (e.g. `ck0_b`), both the `FlopDomain.clock`
  field and the `Crossing.dst_clock` it flowed into carried the
  raw port name instead of the SDC-declared clock name (`ck0`
  from `create_clock -name ck0 [get_ports {ck0_a ck0_b}]`). The
  `--emit-domain-map` JSON's `flop_domains[].clock` and
  `crossings[].dst_clock` then disagreed with each other on the
  same physical domain, and external consumers couldn't join
  either back to the `clocks[]` table. Both `assign_domains` and
  `find_crossings` now take an optional `clock_for_port=` keyword
  (passed `ClockSpec.clock_for_port` at the CLI boundary) and
  normalise the trace result before constructing each `FlopDomain`
  / `Crossing` record. Rule-pack behaviour is unchanged (the rules
  already perform their own port-domain comparison via
  `set_clock_groups`); the bug only surfaced in the consumer-facing
  artefact. Regression pinned by
  `tests/test_bad_async_clock_mux.py::test_q_out_flop_domain_normalises_to_sdc_clock_name`
  and `test_crossings_dst_clock_normalises_to_sdc_clock_name`.

- **`rtl-buddy-cdc render` subcommand** (#162). New CLI command that
  consumes a v1.0 domain map (as produced by
  `--emit-domain-map`) and emits a GitHub-renderable
  ```` ```mermaid ```` flowchart: flops grouped into one `subgraph`
  per clock domain, async-per-SDC crossings drawn as dashed warning
  edges with width annotation, top-level ports surfaced as stadium
  nodes anchored to their declared clock. The renderer is a pure
  function over the existing artifact (no analyzer rerun) and
  matches the discipline of `reporter.py`: deterministic, sorted,
  no I/O outside `cli.py`. Designed for per-fixture documentation
  where `rtl-buddy-view`'s module-level hierarchy collapses flat
  designs to a single box; this fills the flop-level gap without
  expanding the view tool's scope. Today's surface is
  `render --map <path> --format mermaid [-o <out>]`; the
  `RenderFormat` enum is in place for additional formats (e.g.
  dot) to plug in without flag-surface churn.

- **Domain-map: `source_instance_path` on flops and crossings**
  (#136). The `--emit-domain-map` artifact's `flop_domains[]`
  entries now carry a `source_instance_path` sibling to
  `instance_path` — the deepest enclosing SystemVerilog-source
  module instance for the flop, rooted at the design top.
  `crossings[]` mirror this with `dst_source_instance_path` and
  (when the source endpoint is a flop) `src_source_instance_path`.
  Downstream consumers no longer need to re-derive the source
  hierarchy by stripping `$slang$…`/`$procdff$…` leaves themselves
  (see `_flop_resolver` in rtl-buddy-view#27 — this issue is the
  producer-side cleanup that lets the consumer drop it). The
  fields are additive optionals; `schema_version` stays at `"1.0"`.
  Emitted as `null` (never omitted) when the analyzer can't
  resolve the chain — distinct from an older producer that didn't
  emit the field.
- **External reset hints (`--reset-hints FILE.yaml`)** (#129). New
  CLI flag on both ``analyze`` and ``lint`` that loads an external
  YAML declaration of reset-port polarity / synchroniser
  annotations, parallel to the in-RTL ``(* reset_polarity *)`` /
  ``(* reset_sync *)`` SV attributes. Same vocabulary, external
  file when the user can't touch RTL (vendor IP, generated
  wrappers, multi-block boards where one file beats scattered
  attributes). Schema is the ``reset-hints:`` block with optional
  ``ports`` (``name`` / ``polarity`` / ``type`` / ``clock``) and
  ``synchronizers`` (exact ``instance`` or shell-glob
  ``instance_glob``; matched against the resolved hierarchical
  path, so the same hint covers both Yosys-flatten and slang
  cell-name shapes). Schema version pinned by
  ``rtl_buddy_cdc.reset_hints.SCHEMA_VERSION`` (``"1.0"``); strict
  parsing fails loudly on unknown keys / malformed enum values
  with file context. Hints **win** on disagreement with the SV
  attribute path; synchroniser sets union with no precedence
  question. Threaded into ``run_all`` via the new
  ``reset_hints=`` keyword, and into the ``--emit-reset-domain-map``
  pipeline so the artefact reflects the merged view. Gated on a
  new ``[hints]`` install extra
  (``pip install 'rtl-buddy-cdc[hints]'``) — default installs stay
  ``typer``-only; PyYAML pulls in only when the extra is requested,
  matching the ``[slang]`` precedent. Missing-extra path raises
  ``ResetHintsUnavailable`` with the install command, the loader
  raises ``ResetHintsError`` on validation failures (both surface
  through the CLI as exit 2). Schema reference at
  ``wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md``;
  analysis-doc §8.3 promoted from "planned" to a working
  reference.
- **`--emit-reset-domain-map` reset-domain artifact** (#108). New CLI
  flag on both ``analyze`` and ``lint`` that writes a stable v1.0
  JSON sidecar capturing the reset-tree view of the design, parallel
  to the clock-domain map (#106). Payload sections: ``reset_sources``
  (distinct upstream resets, deduped by ``(name, source)`` —
  ``"port"`` / ``"inferred"`` / ``"constant"``), ``reset_synchronizers``
  (one entry per flop in the recognised reset-synchroniser set, union
  of the structural recogniser and ``(* reset_sync *)``-marked
  flops), ``flop_resets`` (per-flop reset assignments with source
  locations), and ``reset_crossings`` (kinds ``async-deassert``,
  ``polarity-mismatch``, ``sync-crossing``, ``comb-driven`` —
  parallel to RDC-001..-004). Port-level ``(* reset_polarity *)``
  declarations surface as ``declared_polarity`` on the
  ``reset_sources`` entry. Schema version pinned by
  ``rtl_buddy_cdc.reset_domain_map.SCHEMA_VERSION`` (``"1.0"``);
  every collection is sorted by a documented key so two builds on
  the same inputs emit the same byte sequence (pinned by
  ``tests/test_reset_domain_map.py::test_deterministic``). Composable
  with ``--emit-domain-map`` — both flags can be passed in a single
  invocation; the shared ``design.top`` / ``design.frontend`` envelope
  lets consumers join the two artefacts safely. ``--no-findings``
  short-circuits rule evaluation when one or both maps are the sole
  deliverable. Schema reference at
  ``wiki/raw/articles/rtl-buddy-cdc-reset-domain-map-schema.md``.
  Immediate consumer: rtl-buddy-view's Phase 3 reset-overlay.
- **`(* reset_polarity *)` SV attribute + RDC-002 port-declared
  polarity variant** (ninth instalment of #107). Top-level reset
  ports can be annotated with ``(* reset_polarity = "low" *)`` /
  ``"high"`` to declare the intended active polarity as authoritative.
  RDC-002 grows a second firing path: when a flop's async reset
  traces back to a declared port and the flop's inferred
  ``ARST_POLARITY`` disagrees with the declaration, the rule fires
  with a message naming the port and both polarities. This catches
  the "designer added a ``posedge rst_n`` flop on a port the rest of
  the design treats as active-low" wiring bug — invisible to every
  prior rule (no clock crossing, no flop→flop reset path). Paired
  fixtures ``bad_marked_reset_polarity`` /
  ``good_marked_reset_polarity`` with a test covering the helper, the
  port-only scoping rule (internal nets with the attribute are
  ignored), and the end-to-end rule path.
- **`find_reset_crossings` + `ResetCrossing` unified API**
  (companion to the RDC family, issue #107). New public surface in
  ``rtl_buddy_cdc.reset_domain`` that emits one record per flop whose
  reset arrival is worth flagging — kinds ``async-deassert``,
  ``sync-crossing``, ``comb-driven``, ``polarity-mismatch`` —
  consolidating the structural facts the RDC rule pack would walk to
  individually. Additive: the per-rule walks in ``check_rdc_00N`` are
  unchanged; this is the surface external consumers (e.g.
  ``rtl-buddy-view``) call when they want a unified reset-domain
  view without rerunning the analyzer.
- **`(* reset_sync *)` SV attribute escape hatch** (#115, eighth
  instalment of #107). Parallel to the existing
  ``(* cdc_sync *)`` / ``(* cdc_gray *)`` annotations. Marks a flop
  as a vetted reset-synchroniser stage even when the structural
  recogniser in ``rtl_buddy_cdc.reset_domain.find_reset_synchronizers``
  wouldn't match — the structural pass deliberately requires a
  constant-fed chain head, so chains whose head's D is fed by an
  upstream signal (rather than a literal constant) are otherwise
  missed. RDC-002 / RDC-004 / RDC-005 skip flops marked with this
  attribute and also skip consumers whose ARST is driven by a marked
  flop's Q (matching the user's intent that "this reset arrives
  cleanly downstream"). New helper
  ``rtl_buddy_cdc.rules.user_reset_sync_flop_names(module)`` mirrors
  the existing ``user_sync_flop_names`` shape. New optional
  ``extra_synchronizers`` parameter on ``find_reset_synchronizers``
  folds user-marked flops into the recogniser's output set.
  Accepted aliases: ``reset_sync``, ``reset_synchronizer``. Coverage
  fixture ``marked_reset_sync`` with a dedicated test module
  (``test_marked_reset_sync.py``) pinning the attribute discovery,
  recogniser overlay, and end-to-end RDC-002 suppression.
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

- **CDC-004 now checks typed port-sourced bus crossings** (#149). The
  rule used to exit early on any port-sourced multi-bit crossing,
  exempting typed top-level buses from the same coherence check
  applied to flop-sourced buses. A typed input could cross into an
  async destination through a per-bit synchronizer and stay silent —
  the input typing suppressed CDC-011 but said nothing about whether
  the bits are sampled coherently. The early-exit is gone; typed
  port-sourced bus crossings now fire CDC-004 unless they're cleared
  by load-enable gating (the gray-coding acceptance paths still
  require a flop source — there is no port-side equivalent for
  asserting a gray invariant). Violation messages render the source
  endpoint as ``"port <name>"`` via ``Crossing.src_name`` instead of
  the (previously assumed) ``src_flop.name``. The new
  ``bad_typed_port_bus_crossing`` fixture exercises the firing case;
  three companion good fixtures (``good_gray_bus_sync_chain``,
  ``marked_single_ff_sync``, ``good_single_source_fanout_sync_chains``)
  preserve coverage of the CDC-004 structural-gray arm, the
  ``(* cdc_sync *)`` single-stage waiver, and the shared-source
  fan-out shape after several positive fixtures were rewritten
  toward shapes that pass more third-party CDC linters cleanly.
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
