# Changelog

All notable changes to `rtl-buddy-cdc` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Compositional per-module boundary analysis** (#261) — *minor:
  additive data-model and CLI surface, no existing field renamed or
  retyped.* Boundary abstraction (#253 / #256 / #257) scales by collapsing
  a subtree to its port boundary, and #259 closed the *silent* half of
  what that costs by **declining** the shapes it could not check —
  multi-clock blocks, and single-clock blocks with ≥2 incoming crossings.
  Sound, but it declined precisely the dense-CDC integration blocks
  abstraction is most valuable on, and it left
  `PortBoundary.synchronised` inert, so every abstracted single-bit input
  fired CDC-001 no matter how well the IP synchronised it.

  The new `rtl_buddy_cdc.compositional` module analyses each boundary
  **module once, on its own internals** — the ordinary pipeline
  (`assign_domains` → `find_crossings` → the rule pack), keyed and cached
  per `(module type, clock-pin → root mapping)` — and lifts the result
  into the summary instead of erasing it.

  It needs the internals, which a `--blackbox` stub does not have (Yosys
  discards the body). The new **`lint --greybox MODULE`** keeps them: a
  plain `setattr -mod -set blackbox 1 <module>` between `proc` and
  `flatten`, so the module still stands as a boundary cell but carries its
  cells. It needs no yosys-slang plugin, unlike `--blackbox`. A zero-cell
  blackbox takes the pre-#261 path unchanged — the entire existing fixture
  suite is untouched proof.

  What that buys:

  - **`synchronised` goes live, proven.** A width-1 input port whose bit
    has exactly one reader — a 1-bit flop's `D` in a known domain — and is
    not also an output bit gets its chain measured. The *depth* (not just a
    boolean) is published to the rule pack, so CDC-001 / CDC-002 / CDC-003
    / CDC-005 behave at the boundary as they do flat: quiet at depth ≥ 2,
    and `--sync-depth 3` still fires CDC-002 on a 2-deep internal chain. A
    `USER_SYNC_ATTRS` tag on the first stage is honoured as the explicit
    promise. Tie-offs, comb bypasses, extra loads, multi-bit ports and
    feed-throughs all defeat the proof — `synchronised=True` is the only
    lever in this model that can make the tool under-report, so it is set
    only when proven.
  - **Findings inside a block are reported.** Once per module type, with
    the instances they cover named in the message and `cell_name`
    re-anchored on a parent instance so they resolve to a source location
    and are waivable by boundary instance. Two instances of one IP yield
    one finding, not two — the analyse-once contract, visible.
  - **Internal reconvergence is re-raised at the boundary.** The
    reconvergence gate (#259 FIX 3) now stands down per instance for an
    analysed module; CDC-005 fires with the recombination point named.
  - **Multi-clock blocks are summarised per port**, each stamped with the
    domain that really captures or launches it, so the star-collapse
    becomes per-domain instead of one hub. The `multi_clock_blackbox`
    fixture that #259 had to decline now reports the same CDC-004 the flat
    run does.
  - **A silent drop `--blackbox` could not see is now caught.** A flop
    clocked from a module pin no classifier recognises resolves internally
    to a name that is a declared clock nowhere, so `are_async` filters its
    crossings away. Pin inspection read such a module as *single-clock* and
    abstracted it with no diagnostic at all. It is now declined loudly.

  Three cases still decline, each with its own `CDC-BBX` wording: an
  internal flop that lands on no parent clock root (above), an input
  captured in two internal domains, and a multi-clock module with no
  internal registers. Three rules are excluded from the internal pass
  because their predicate is literally "a top-level port of the design" —
  CDC-006, CDC-011, RDC-008 — and their subject matter is reported by the
  parent at the boundary port instead.

  Data model (all additive, all defaulted): `PortBoundary.sync_depth` /
  `.user_synchronised`; `BoundarySummary.internal_analysed` /
  `.reconvergent_inputs`; `CompositionStats.analysed_modules` / `.lifted` /
  `.ambiguous_input_modules` / `.unresolved_internal_modules`. The
  downstream JSON contract (`summary.violations` / `.suppressed` /
  `.crossings`) is untouched, and no new runtime dependency. `domain
  .filter_async` is the async-crossing predicate lifted out of `cli` so the
  per-module pass and the top-level run cannot disagree; `cli._filter_async`
  is now a thin alias.

  New fixtures `bbx_input_sync`, `bbx_input_sync_broken`,
  `bbx_shared_internal_violation`, `bbx_residual_declines`, each shipping
  three netlists from one source (`.flat.json` / `.grey.json` / `.json`),
  plus `.grey.json` companions for `multi_clock_blackbox`,
  `reconvergence_two_inputs`, `safe_single_input`,
  `single_clock_leaf_abstract`, `shared_subtree_compose` and
  `clock_output_blackbox` — the last of which pins that the #273
  clock-output decline survives compositional analysis unchanged, because
  analysing a module's body says nothing about the clock network it
  forwards to its parent.

  See `wiki/raw/articles/rtl-buddy-cdc-architecture.md` §4.8 / §4.9 / §7.4
  for the model, the proof obligations, and the parity relation the
  abstracted run guarantees against a flat one.

- **In-RTL pragma scanner** (`rtl_buddy_cdc.pragma`, #41). A
  suppression can be written next to the RTL it applies to instead of
  in an external waiver file whose regexes chase synthesis-generated
  cell names:

  ```systemverilog
  // rbcdc: disable-rule CDC-001
  // rbcdc: disable-rule CDC-001,CDC-002  hand-reviewed handshake
  /* rbcdc: disable-rule CDC-005 library cell */
  ```

  `rbcdc:` is this tool's magic-comment namespace (the org-wide
  convention gives each rtl-buddy tool one — `rbsch:`, `rbxeno:`, …).
  It is the only accepted spelling; there is no SV-attribute form.

  `pragma.scan(sources)` reads the sources as **text** — never via
  Yosys or slang — and returns ordinary `waivers.Waiver` records, one
  per (pragma × rule id), scoped to the file's basename with the free
  text after the rule list as the reason. `Waiver` gains an `origin`
  field: `None` for a waiver-file entry (so `source_line` is a line in
  that file), the source path for a pragma (so `source_line` is the
  line of the pragma in the RTL).

  Pragmas are applied on the **`lint`** path (the one that starts from
  sources): `lint` scans its sources and prepends the results to
  whatever `--waivers` produced, so an inline suppression wins over a
  broad file regex. `analyze` is unchanged — it consumes an
  already-elaborated netlist and has no sources to scan, which its
  `--help` now says explicitly.

  A pragma matches by **source location**, not by name: it suppresses
  findings the analyzer attributes to the file the pragma lives in,
  and is never tried against a cell name or message text. A finding
  whose location can't be resolved is never waived by a pragma.
  `waivers.apply` grew a keyword-only `source_file` resolver for this
  (the location lives on the offending cell, so only the CLI — which
  holds the `Module` — can supply it).

  Reporting distinguishes the two producers. Text:

  ```text
  Suppressed by waivers (1)
    CDC-001  hand-reviewed: q_out is quasi-static  (pragma rtl/dut.sv:26)
  ```

  JSON `suppressed[].waiver` gains an **`origin`** key — `null` for a
  waiver-file entry (so `source_line` is a line in the `--waivers`
  file), the source path for a pragma (so `source_line` is the line of
  the pragma). Purely additive: a consumer reading the pre-existing
  fields is unaffected, and `summary.violations` /
  `summary.suppressed` / `summary.crossings` are untouched.

  Fixture `pragma_waived_single_ff` is the end-to-end case: a CDC-001
  crossing waived in place, `lint` exits 0 with the finding on the
  suppressed list, `analyze` over the same committed netlist still
  exits 1.

  **Block scoping** (#43): an `enable-rule` closes the region a
  `disable-rule` opened, so a suppression can cover one always-block
  instead of a whole file.

  ```systemverilog
  // rbcdc: disable-rule CDC-001 vetted by hand
  always_ff @(posedge dst_clk) q <= src_q;
  // rbcdc: enable-rule CDC-001
  ```

  The region is half-open — `[disable_line, enable_line)` — so the
  `enable-rule` line is outside it. Pairing is per rule id, so blocks
  for different rules interleave without interfering; re-disabling an
  open rule ends the running region and starts a fresh one (the newer
  reason applies from there); a stray `enable-rule` is ignored; and a
  `disable-rule` with no `enable-rule` runs to the end of the file —
  the file-scoped form, unchanged.

  `Waiver` gains `start_line` / `end_line` (both null on a
  waiver-file entry, which has no line scope) and `waivers.apply`'s
  resolver widened from a file to a `SourceRef(file, line)` — the
  keyword is now `locate`. A finding located to a file but not a line
  falls back to file scope: a range check the analyzer can't perform
  shouldn't silently swallow the suppression.

  A closed block reports its range, which is what makes a block-scoped
  waiver visibly narrower than a file-scoped one:

  ```text
  Suppressed by waivers (2)
    CDC-001  vetted by hand              (pragma rtl/dut.sv:37-42)
    CDC-004  whole file, generated code  (pragma rtl/gen.sv:3)
  ```

  JSON `suppressed[].waiver` carries `end_line` alongside `origin`.
  Fixture `pragma_block_scope` is the paired case: two identical
  CDC-001 crossings, the one inside the block suppressed and the one
  after it kept, so the run still exits 1.

- **`--blackbox` now refuses a module that drives a clock output**
  (`CDC-BBX`, #273). The boundary-soundness check declined a candidate
  it could not prove single-clock, but happily *accepted* a single-clock
  module with a **clock output port** — one that generates or forwards a
  clock consumed elsewhere. Blackboxing it silently elided the clock
  generation/forwarding network: the forwarded clock left an opaque
  boundary output, its downstream consumers went domain-unknown (or
  vanished outright when they were abstracted too), and the still-visible
  muxes / ICGs / buffers feeding the boundary lost their
  clock-distribution status, so CDC-008 ("clock used as data")
  false-fired on them. A hierarchical clock-forwarding mesh was the worst
  case: blackbox the tile and the whole clock sub-network disappeared
  with no diagnostic.

  Such a candidate is now **declined**, exactly like a
  not-provably-single-clock one — same `CDC-BBX` id, same `error`
  severity, same report path, same waiver
  (`waive CDC-BBX <instance-regex>`). Only the message differs, and it
  names the offending port:

  ```text
  blackbox `u0` (`clkfwd_tile`) drives a clock output `clk_out` — clock
  generation/forwarding would be elided; flatten it or analyse standalone
  (waive CDC-BBX if intentionally out of scope here).
  ```

  `abstract.clock_driving_output_ports` is the (pure) detector: an output
  bit "drives a clock" when it reaches — directly, or through a
  `--clock-trace-depth`-bounded forward walk over ordinary combinational
  cells — a flop `CLK` / `C` pin, an SDC-declared clock net
  (`create_clock` port or `create_generated_clock` internal-pin target),
  or a **clock input pin of another blackbox / boundary instance**. That
  last sink kind reads `abstract.blackbox_clock_pins_by_module`, the
  union of a module type's per-instance traced clock pins, because on a
  forwarding mesh the downstream tile's `clk_in` is driven by an opaque
  boundary output and cannot be traced to a declared clock on its own —
  the upstream instance of the same type is what proves the pin is a
  clock pin. No pin-name guessing is involved. The walk stops at flops (a
  `D` pin is a data sink) and at other boundaries.

  The verdict is **per module type** — any qualifying instance declines
  the type, so a tile whose top instance leaves `clk_out` unconnected is
  declined too — and is decided in a `compose_boundaries` pre-pass ahead
  of the `--sync-primitive` / XPM path, whose promise is about the *data*
  crossing. `CompositionStats` gained `clock_output_ports` (the
  `(module type, output port)` pairs) and `clock_output_ports_of()`.
  A module whose outputs carry only data — the common case — is never
  declined by this check; the new `clock_output_blackbox` fixture pins
  both directions in one netlist (two declined clock-forwarding tiles, an
  accepted data-only core).

  No schema change. Effect on existing runs: a design that blackboxes a
  clock-generating or clock-forwarding module and previously reported
  clean now reports a `CDC-BBX` error per instance of it — blackbox one
  level deeper (the clock-output-free datapath core) or waive it.

- **CDC-023 — clock net driven by a combine of two declared clocks**
  (#269). A clock gate (`$and` / `$or` / `$xor` …) or a clock-path
  transparent latch (`$dlatch` / `$_DLATCH_*`) whose legs carry two or
  more *distinct declared clocks* mixes clock domains combinationally:
  the net glitches, runs at no declared frequency, and strands every
  flop behind it. The clock-root tracer has **declined** on this shape
  since #263 — which removed the silent-mislabel hazard but left the
  flops in the generic `domain_unknown` tally with no cause attached, so
  a genuine two-clock combine looked exactly like a clock tree deeper
  than `--clock-trace-depth`. CDC-023 names the cause: the combining
  **cell**, the combined **net**, and the two **clocks**, so it can be
  fixed or waived instead of hunted for in the under-resolution list.

  The finding is emitted *by* the decline rather than re-derived from
  it. `domain._pick_combining_root` gained an `on_decline` callback
  invoked at the single line that returns `None`; `trace_clock_root`
  threads a matching `on_combine` recorder; and the new
  `domain.find_clock_combines` runs the ordinary per-flop clock-root
  walk with that recorder attached, returning one `ClockCombine` record
  per combining node. The rule therefore fires **iff** the tracer
  declined — one predicate, one traversal, no second implementation to
  drift out of step. The declared-clock predicate itself moved out of
  `assign_domains` into `domain._clock_identity_fn` so both callers
  share one copy, which is what makes the #267 hardening apply to the
  rule for free: a plain enable port is not a declared clock, and nor is
  the `<unconstrained>` sentinel
  `sdc.synthesize_unconstrained_inputs` stamps on untyped input ports,
  so a normal ICG never fires. A clock **mux** never fires either — a
  mux *selects* one clock rather than combining them.

  Severity `warning` (`--strict` → error): combining two clocks is
  nearly always a bug, but the shape also covers deliberate,
  characterised test/debug clock chopping, and a new rule id shouldn't
  turn a previously-clean run into a hard error the day it lands.
  Waivable by rule id like any other rule.

  No schema change — an additive rule id, reported through the normal
  `Violation` path (text / JSON / SARIF). The existing
  `clock_combine_gate` / `clock_combine_latch` /
  `clock_combine_generated` fixtures now fire CDC-023; `icg_port_enable`
  stays clean, and the new `tests/test_cdc_023_clock_combine.py` loads
  every fixture through `synthesize_unconstrained_inputs` so the
  sentinel path is really exercised. Designs that combine two declared
  clocks and previously reported clean (bar an unexplained
  `domain_unknown` count) will now report a CDC-023 `warning`.

- **DFT / scan-mode SV attribute recognition** (#44) — *patch:
  recognition only, no behaviour change.* `SCAN_MODE_ATTRS`
  (`scan_en` / `scan_mode` / `test_mode` / `dft_scan_en`, matched
  case-insensitively) and `rules.scan_mode_port_names(module)` name the
  top-level input ports a DFT insertion flow marks as test-mode
  controls — the select of the classic scan clock mux
  `$mux(S=scan_en, A=func_clk, B=scan_clk)` that mixes two async clocks
  into one `CLK` net and makes every crossing behind it read as a CDC
  failure.

  Nothing in the rule pack consults the helper yet, so **no fixture's
  findings move**; issue #45 wires it in behind an opt-in
  `--ignore-scan-mode`. Deliberately split: the recognition is
  uncontroversial, the suppression is a soundness decision that
  deserves its own review.

  Both frontends already carry the attribute through — Yosys preserves
  a declaration attribute on the input port's netname, and the slang
  frontend's `_collect_port` forwards `PortSymbol` attributes onto the
  same netname (the #38 fix, which happened to cover inputs as well as
  outputs). Pinned for inputs specifically by
  `test_slang_lowering.py::test_input_port_scan_mode_attribute_reaches_netname`,
  so a frontend regression can't silently make the recognition a
  Yosys-only feature.

- **`--ignore-scan-mode`: opt-in DFT scan-path suppression** (#45) —
  *minor: additive `Crossing` field, additive JSON summary key, new CLI
  flag; nothing renamed, retyped, or removed.* Wires #44's recognition
  into the rule pack.

  A crossing is now **tagged** `scan_mode` when its *destination*
  flop's `CLK` fanin passes through a mux or gate whose control traces
  back — through inverters and comb logic — to a scan-tagged input
  port. `--ignore-scan-mode` (on both `analyze` and `lint`) makes the
  rule pack skip the tagged crossings. **Off by default**: without the
  flag every fixture's findings are exactly what they were, which is
  the conservative side to fail on, since a DFT structure that is
  genuinely live in mission mode is a real hazard.

  Detection rides the walk that already resolves the flop's domain
  rather than a second traversal. `trace_clock_root` gains
  `on_clock_control`, a `ClockControlSink` observer invoked from the
  mux / gate / latch clauses of `_trace` with the bits that *steer* the
  selection (a `$mux`'s `S`, a gate's or latch's non-clock legs);
  `domain.find_scan_mode_flops` attaches it and applies an injected
  `is_scan_control` predicate, which `rules.scan_mode_clock_select_flops`
  supplies. Same construction and same reason as #269's `on_combine`
  recorder: "this flop is clocked through a scan mux" and "the tracer
  walked a mux to get here" are one event, not two predicates that
  agree today. Purely observational — the walk's outcome is unchanged.

  Tag, don't drop: `Crossing.scan_mode` is additive and defaults False,
  the crossing is still emitted and still counted in
  `summary.crossings`, and only `run_all`'s single pre-dispatch filter
  skips it. Filtering once on the shared list rather than per-rule is
  what makes the promise checkable — every crossing-consuming rule
  honours it identically.

  Nothing is silent. `summary.scan_mode_suppressed` (new, `0` without
  the flag) counts the skipped async crossings, the text report prints
  a matching tally line, and the per-crossing `scan_mode` tag appears
  in the JSON crossing records and the `--verbose` listing
  unconditionally — so even a run that suppresses nothing is auditable.

  Two deliberate non-goals, conservative rather than silent: CDC-010
  and CDC-023 walk the clock network rather than the crossing list and
  are untouched by the flag (excusing the data paths behind a scan mux
  is not the same claim as blessing the mux), and the compositional
  per-module pass (#261) runs its own `run_all`, so a scan mux inside
  an abstracted module still fires.

  New fixtures `good_scan_mode_ignored` (all-scan: CDC-001 without the
  flag, clean with it) and `bad_scan_mode_functional_crossing` (the
  same mux plus an ordinary functional crossing that must survive the
  flag). Downstream `rtl_buddy` needs no change — the flag is opt-in
  and the three contract keys are untouched.

### Fixed

- **CDC-011 now sees an untyped sync reset on a dedicated `SRST` pin**
  (#272). Whether a missing `set_input_delay -clock` on a *synchronous*
  reset got reported depended on a synthesis-pass detail. The crossing
  model is `D`-pin-scoped — `domain.find_crossings` seeds and terminates
  its walk on flop `D` pins — and CDC-011 consumes port-sourced
  crossings. So when the lowering folded the reset into a `$dff` D-cone
  (a reset mux), CDC-011 fired; when `opt_dff` folded the same reset
  into a `$sdff` `SRST` pin, there was no crossing to consume and the
  port vanished from the report entirely. RDC-003 didn't cover the gap
  either: it keys on the reset's *source domain* versus the consumer
  clock, and an untyped port has no named source domain.

  `check_cdc_011` now supplements the crossing-derived destinations with
  a direct `SRST`-pin walk (`_backward_port_fanin` from every `SRST`
  connection), merging both destination sets per port before severity is
  decided. Same rule id, same severity, byte-identical message under
  either lowering — the new `bad_untyped_sync_reset_srst` /
  `bad_untyped_sync_reset_mux` fixture pair pins that parity, and
  `good_typed_sync_reset` pins the textbook fix (type the reset) as
  silent.

  Deliberately scoped to **synchronous** resets. An async reset pin
  (`ARST` / `CLR` / `ALOAD`) is legitimately untimed, so
  `set_input_delay -clock` would be meaningless advice there, and the
  RDC family (RDC-001 / RDC-006 / RDC-008) already owns the async-reset
  failure modes.

  No schema change: this adds findings under an existing rule id.
  Designs with an untyped sync-reset port that previously reported clean
  will now report a CDC-011 `warning` (or `error` if the port also
  reaches a second clock domain).

- **The inferred-clock advisory no longer re-reports a clock declared on
  an internal pin** (#270). `domain.find_inferred_clock_candidates`
  excludes nets that are already declared clocks, but it only knew two
  declaration forms: a top-level input port, and a
  `create_generated_clock` target (`ClockSpec.pin_clocks`). A plain
  `create_clock [get_pins <net>]` on an internal net is a third — the
  parser files its target in `Clock.ports`, readable only through
  `ClockSpec.clock_for_port` — so a divided or forwarded clock the user
  *had* declared that way could still surface as an
  `inferred_clock_candidates` entry telling them to declare it.

  The exclusion now also consults `clock_for_port` (via the new
  `domain._declared_clock_bits`), the same declared-clock lookup the
  `clock_identity` combine predicate in `assign_domains` uses. The
  lookup is asked only about nets that already clear the fanout floor,
  and the `<unconstrained>` sentinel is never treated as a declaration.
  New fixture `declared_pin_clock` pins the case alongside the existing
  `inferred_fwd_clock` (undeclared → reported) and `inferred_gate_clock`
  (gate-driven → reported).

  Advisory-only, as the whole detector is: it changes no domain,
  crossing, or violation, and `summary.*` counts are untouched. Affected
  runs simply lose a false-positive advisory line.

## [0.4.0] — 2026-08-18

### Added

- **Single-compilation-unit Yosys Slang elaboration** (#277). The new
  `rtl-buddy-cdc lint --single-unit` option forwards `--single-unit` to
  `read_slang`, allowing project filelists whose packages, macros, or other
  compilation-unit state intentionally span source files to use the
  yosys-slang frontend without wrapper files or source rewrites.

- **XPM CDC macro recognition** (#275). Real FPGA designs synchronise
  with a vendor macro rather than a hand-rolled 2FF chain, and the
  Xilinx XPM CDC family (`xpm_cdc_single`, `xpm_cdc_array_single`,
  `xpm_cdc_gray`, `xpm_cdc_handshake`, `xpm_cdc_pulse`,
  `xpm_cdc_sync_rst`, `xpm_cdc_async_rst`) is the dominant case. Its
  sources ship inside the vendor install tree, so a filelist built from
  project RTL carries only the instantiation — the analyzer saw a
  bodyless, **dual-clock** blackbox, decided it was "not provably
  single-clock", and declined it. Two failures followed: one `CDC-BBX`
  error per instance (a 40-macro design needed 40 waivers), and — less
  visible — the crossing through the macro **vanished**, because a
  declined instance seeds neither a boundary source nor a boundary sink.

  The new `rtl_buddy_cdc.primitives` registry recognises the family **by
  module name**. That is the deliberate choice: the name is the
  contract (a fixed, documented, versioned library whose ports are
  rigidly `src_*` / `dest_*` and whose clocks are `src_clk` /
  `dest_clk`), and it is the only route that works when the sources
  aren't there. A recognised instance is summarised by
  `abstract.summarise_sync_primitive` as a *synchroniser*: each output
  port is stamped with the domain that really drives it and marked
  `synchronised=True`, and no virtual sink is seeded on its data inputs.

  Recognition deliberately does **not** become a blind spot — because
  each output carries its true domain, a `dest_out` consumed by a flop
  in some *third* domain is still reported by the ordinary
  `dst_clock != src_clock` test. We accept the crossing the macro
  handles and keep the one it doesn't.

- **CDC-022 — recognised CDC primitive with insufficient sync depth**
  (#275). The blackbox analogue of CDC-002. A macro carries its stage
  count as a *parameter* (`DEST_SYNC_FF`, plus `SRC_SYNC_FF` on
  `xpm_cdc_handshake`), not as a chain the analyzer can walk, so once
  the macro is recognised CDC-002 can never speak to depth again.
  CDC-022 reads the parameter, firing `warning` (`--strict` → error)
  when it is below `--sync-depth`. It reads only `Cell.type` /
  `Cell.parameters`, so it works even when the macro arrived as a bare
  unresolved cell with no blackbox sibling; an XPM instantiation that
  leaves the parameter at its default is checked against UG974's
  documented default of 4 rather than skipped. **New rule id — nothing
  was renamed**; the rule-id set is a downstream contract.

- **`--sync-primitive MODULE`** (repeatable, #275). Registers a
  site-local or other-vendor CDC macro for the same treatment. Mirrors
  the existing repeatable `--blackbox` option — no new config file, no
  new schema. Registered names get no XPM port-naming promise, so all
  of their outputs are attributed to the destination clock (the
  conservative reading). A registered macro whose destination clock
  can't be identified is **not** silently vouched for: it falls through
  to the generic path and is declined as before.

### Fixed

- **`(* ASYNC_REG *)` never matched the Xilinx spelling** (#275).
  `USER_SYNC_ATTRS` has carried an `async_reg` alias since the marker
  plumbing landed, precisely to honour the Xilinx synthesis attribute —
  but Xilinx (and the XPM macro sources) write
  `(* ASYNC_REG = "TRUE" *)`, and Yosys preserves attribute names
  verbatim, so a case-sensitive match never fired on the one idiom the
  alias existed for. `user_sync_flop_names` now matches attribute names
  case-insensitively. This also gives the "user *does* have the XPM
  sources in the filelist" route for free, with no XPM-specific code.

## [0.3.3] — 2026-06-17

### Fixed

- **CDC-012 false positive on `(* cdc_handshake *)` primitives.** CDC-012
  (functional data-hold on a gated multi-bit crossing) is silenced when
  it finds a synced-back ack in the source flop's register-neighbourhood
  (`_has_dst_to_src_feedback`), but that structural walk can't always see
  the in-primitive ack of a req/ack handshake — it depends on how the
  toggle/ack flops lower, and differs across frontends (it was reachable
  under the yosys frontend but not under pyslang for the same design).
  A four-phase req/ack primitive whose participants are tagged
  `(* cdc_handshake *)` therefore tripped CDC-012 even though the
  handshake's `src_ready` backpressure holds the payload — exactly
  CDC-012's hold guarantee. `check_cdc_012` now honours the annotation
  directly (skip when either endpoint of the crossing is tagged), the
  same opt-in the attribute already provides for CDC-001/013/014/020
  (#247). Covered by extending the `(* cdc_handshake *)` suppression
  parametrisation to CDC-012's `bad_functional_datahold_enable` fixture.

## [0.3.2] — 2026-06-16

### Changed

- **Frontend leniency for standalone-block lint.** Both slang-based
  frontends now tolerate two constructs the built-in `read_verilog`
  frontend already accepts, so switching a design onto a slang frontend
  doesn't newly reject structurally-valid sources:
  - The **pyslang frontend** (`--frontend slang`) sets
    `AllowTopLevelIfacePorts` (explicit; already a pyslang default) and
    `AllowUseBeforeDeclare`. This lets a block with an **unconnected
    top-level SystemVerilog interface port** (e.g. `apb_intf.subordinate
    apb`) be CDC-linted on its own — the yosys `read_slang` path can't
    do this at all, since a yosys netlist has no interface ports, so
    interface-bearing block tops must use `--frontend slang`.
  - The **yosys `read_slang` frontend** passes `--allow-use-before-declare`
    so a module-item reference to a net declared later in the same
    module no longer fails elaboration. (`--allow-toplevel-iface-ports`
    is *unsupported* by yosys-slang and is intentionally not passed.)
- **Clock-combining nodes decline instead of silently picking one leg**
  (#263 soundness). When a clock-network gate (`$and`/`$or`…) or a
  clock-path transparent latch (`$dlatch`/`$_DLATCH_*`) combines two
  **distinct declared clocks** on its legs (e.g. `D=clkA, EN=clkB`), the
  clock-root tracer now returns `None` (leaving the flop
  `domain_unknown`, surfaced by the under-resolution report) rather than
  resolving the flop to the first leg. Such a cell genuinely mixes clock
  domains, so asserting one leg silently mislabels a flop whose clock
  toggles on both. The decline is gated on a `clock_identity` predicate
  (built from the SDC), so the common, safe ICG — one clock plus a
  *non-clock* enable port — still resolves; only a real two-declared-clock
  combine declines, and a clock **mux** (which selects, not combines) is
  unaffected. New fixtures `clock_combine_latch`, `clock_combine_gate`
  (decline) and `icg_port_enable` (the regression guard: a port-enabled
  ICG must still resolve). This supersedes the previous first-leg-wins
  behaviour on multi-clock combines; single-clock results are unchanged.

### Added

- **Inferred-clock candidates** (#263). A common cause of
  under-resolution is an undeclared internal generated clock — a
  divided / forwarded clock the user forgot to declare with
  `create_generated_clock`. `analyze` now reports each internal net that
  drives ≥4 flop `CLK` pins from a flop `Q` or a clock-gate / ICG / latch
  output and is not already a declared clock (port or
  `create_generated_clock` target). JSON gains an additive
  `inferred_clock_candidates` list (`{driver, driver_kind, fanout,
  example_sinks}`); the text report adds a cyan `ⓘ` advisory line per
  candidate. This is **advisory only**: it is computed from the netlist
  and SDC pin map, never feeds back into domain assignment or crossing
  detection, and so changes no domain, crossing, or violation — a flop
  behind an undeclared internal clock stays `domain_unknown` unless a
  real clock-root trace already resolves it (the divider / latch clauses
  of `trace_clock_root`). Auto-assigning a clock identity from the
  fanout heuristic alone is deliberately forbidden — it could make two
  async groups read same-domain and silently drop a crossing. New
  fixture `inferred_fwd_clock` (a divide-by-2 toggle flop clocking a
  four-flop bank with no `create_generated_clock`) is flagged as a
  candidate while its bank flops still resolve to `clk_a` via the
  divider trace, and carries a real `clk_a → clk_b` crossing as a parity
  anchor. The key is deliberately not pinned in `JSON_CONTRACT`.
- **Configurable clock-trace depth** (#263). The clock-root tracer's
  hop budget — fixed at 16 — is now exposed as `--clock-trace-depth N`
  on both `analyze` and `lint` (default 16; threaded through
  `assign_domains(..., max_depth=N)` / `find_crossings(..., max_depth=N)`).
  A deep clock tree (a long divider / buffer / ICG chain) can exceed
  16 hops and leave its downstream flops domain-unknown (visible in
  `summary.domain_unknown`); raising the budget resolves them without a
  code change. The change is monotone — a larger budget only ever
  resolves **more** flops, never fewer — so the default leaves every
  result identical. New fixture `deep_clock_divider_chain` (a 30-stage
  ripple divider) is domain-unknown at the default and resolves at
  `--clock-trace-depth 40`, and pins crossing/violation parity across
  depths. `--clock-trace-depth` threads to **every** clock-root trace
  on a run — the boundary-abstraction decision
  (`compose_boundaries` / `summarise_subtree` / `_instance_clocks`),
  the clock-network surface (`find_clock_network_crossings`), and the
  rule-context domain view (`run_all` / `_build_context`) — so the
  abstraction decision and the crossing walk always resolve the same
  clock roots. This keeps the opt-in high-depth mode sound: a
  dual-clock blackbox whose second clock pin sits beyond 16 hops is
  correctly declined (not abstracted away) when the depth is raised,
  instead of silently dropping its internal crossing.
- **Under-resolution visibility** (#263). A flop whose clock root the
  tracer cannot resolve is excluded from crossing detection; on large
  netlists this silently shrank coverage with no diagnostic. The
  reporter now surfaces it (report-only — no classification changes):
  JSON gains `summary.domain_unknown` (int, pinned in `JSON_CONTRACT`)
  and a bounded `domain_unknown_flops` sample list; the text report
  emits a prominent `⚠ N of M flops have unresolved clock domain —
  excluded from CDC analysis` line.
- **Clock-path transparent latch resolution** (#263). The clock-root
  tracer now follows a clock routed through a `$dlatch` / `$_DLATCH_*`
  (a latch-based ICG / clock-path latch). When a flop's `CLK` net is
  driven by a latch `Q`, the walk explores the latch's data pin (`D`)
  and enable pin (`EN` coarse / `E` gate-level) and returns whichever
  leg resolves to a clock root — the clock can enter on either pin
  depending on the ICG coding style. Such flops were domain-unknown
  before and are now resolved. This is **clock-resolution only**: latch
  transparency never reaches data-path crossing detection, so CDC-017
  (transparent latch in a CDC path) and every data-path crossing fire
  exactly as before — a data-path latch stays an opaque, flagged
  endpoint. The first-resolves-wins behaviour on a pathological
  clock-*combining* latch (two distinct clock roots on `D` and `EN`)
  matches the existing two-input-gate clause and is documented in the
  architecture spec §5. New fixture `clock_through_latch` exercises both
  the `D`-leg and `EN`-leg ICG styles and pins crossing/violation parity
  with the latch clause disabled. The `bad_unresolved_clock_latch`
  fixture (a clock-path latch) consequently now resolves to `clk_a`
  (`domain_unknown == 0`); the durable `domain_unknown > 0` anchor moved
  to `deep_clock_divider_chain`.
## [0.3.1] — 2026-06-16

### Changed

- **Coverage ratchet raised 93 → 96.** `tests/test_cov_rules_{c,d}.py`
  cover the rule pack's remaining defensive guards and edge branches —
  lazy-context (`ctx=None`) paths, the no-SDC async fallback, and the
  structural-helper early returns (empty/constant bits, missing CLK,
  no-fanin/feedback, polarity-suffix decoding, reset-tree truncation) —
  lifting `rules.py` from 90% to 96.57% and the project TOTAL from
  95.30% to 97.34%. The `--cov-fail-under` gate in the `pytest (with
  slang)` job moves to 96, restoring ~1pt of headroom under the
  measured TOTAL.

### Fixed

- **CDC-001 / CDC-002 false positive on packed shift-register
  synchronisers** (#264). A multi-flop synchroniser written as a single
  packed shift register — `reg [N-1:0] s; s <= {s[N-2:0], d}` with the
  output tapped from `s[N-1]` — lowers to one multi-bit `$dff` after
  `proc; flatten`. The synchroniser-depth walk hops between *separate*
  1-bit flop cells, so the intra-cell shift was invisible: it stopped at
  the multi-bit head and reported depth 1, firing a false CDC-001 ("no
  second-stage synchroniser") on every such instance — and skewing the
  CDC-002 depth gate. `_sync_chain_depth` now recognises the packed
  idiom: a multi-bit flop whose `D` vector is, lane for lane, either one
  of the flop's own `Q` bits (`D[i] == Q[j]`, an internal shift tap) or —
  for exactly one lane — an external bit (the freshly sampled crossing).
  It follows the per-lane shift from that single external input to the
  terminal tap and counts the effective depth, so the packed form is
  accepted on its own merits, identically to the separate-flop form.
  Genuine bus crossings, gray counters, and mux-gated registers are
  unaffected (they don't match the self-shift structure), and the walk's
  "exactly one reader" rule still ends the chain when an intermediate
  stage is tapped early — a packed register whose first stage is used is
  still a depth-1 CDC-001. New `good_packed_shift_sync` /
  `bad_packed_first_stage_used` fixture pair.

## [0.3.0] — 2026-06-16

### Fixed

- **Blackbox / auto-abstract soundness** (#259 audit). Four silent
  false-negatives in the boundary-abstraction path are closed; all are
  conservative — the analyzer never silently drops or downgrades a real
  CDC hazard:
  - **Multi-clock subtree no longer abstracted as single-clock.** The
    clock determination for a blackbox instance now inspects **all** of
    the module's input ports — a port is a clock pin if its name is in
    the allow-list, or it is conventionally clock-named *and* its driver
    traces (clock-network-only, no flop-divider step) to a declared
    clock. The full set of distinct clock roots flows to
    `is_single_clock_subtree`, so a dual-clock IP whose clock pins are
    `wr_clk` / `rd_clk` (or `clk_a` / `clk_b`, outside the name
    allow-list) presents ≥2 roots and is **declined**, instead of
    collapsing to one domain and silently abstracting away its internal
    clkA→clkB crossing. The traced clock-pin set also excludes clock pins
    from input-sink seeding regardless of their name. New
    `multi_clock_blackbox` fixture pair.
  - **Declined / opaque blackboxes are a waivable error.** A blackbox the
    summariser leaves opaque (multi-clock / unresolved, or
    reconvergence-unsafe per below) is an unanalysed boundary — a coverage
    gap. Each such instance is emitted as a per-instance **`error`** with
    `rule_id = "CDC-BBX"` (`blackbox \`<inst>\` left opaque — …`), so the
    run **fails by default** (exit 1) rather than passing with only a
    warning. Intentional opacity (a separately signed-off IP) is
    acknowledged by waiving it: `waive CDC-BBX <instance-regex>`. The
    silent drop becomes a fail-by-default, explicitly-acknowledged one.
  - **Reconvergence-unsafe single-clock blocks are refused.** A
    single-clock block that *is* abstracted but has ≥2 distinct
    foreign-domain crossings entering **distinct** input ports can hide
    an internal reconvergence (CDC-005) the flat design would flag. The
    new pure `hierarchy.reconvergence_unsafe_instances` detects this
    after `find_crossings`; such instances are removed from the boundary
    map, `find_crossings` is re-run so they become opaque, and each is
    emitted as the same waivable `CDC-BBX` error (`… has crossings into N
    input ports; reconvergence among them cannot be checked …`). A
    single multi-bit bus on one port stays safe. New
    `reconvergence_two_inputs` (unsafe) and `safe_single_input` (parity
    guard) fixture pairs.
  - **CDC-008 blackbox exemption narrowed to clock pins.** The
    clock-as-data exemption for a blackbox instance is now per-CLOCK-PIN
    (the traced determination, or the `_CLOCK_PIN_NAMES` fallback) rather
    than whole-instance, so a clock net wired into a genuine **data**
    input of a blackbox still fires CDC-008.

### Changed

- **`find_crossings`: lane-aware data fanout — O(W²) → O(W) on wide buses**
  (#258). The crossing BFS pushed each bit through a consumer cell to *all*
  of that cell's output bits, discarding the input lane index. On a width-`W`
  datapath bus a single source bit therefore fanned across the whole bus, and
  every bus bit re-walked the same cone — ~`W²` work per cell. For
  width-preserving bitwise / mux-data cells, input lane `idx` now propagates
  only to output `Y[idx]` (`_lane_targets`); the all-outputs walk is kept as a
  sound fallback for bit-mixing cells (adders, shifts, reductions) and for
  ports whose width doesn't match `Y` (e.g. a mux select), so no real
  cross-lane path is dropped. The set of reported crossings is unchanged —
  verified across the full fixture suite plus two new `wide_bus_*` fixtures (a
  lane-aligned `$and` bus and a lane-mixing `$add` bus). A large multi-clock
  fabric that previously did not complete in over two hours now analyzes in
  seconds.
### Added

- **Hierarchical / compositional boundary analysis** (#257, CDC-scaling
  epic #253 phase 3). A block instantiated N times is now **analysed
  once**: pure `hierarchy.compose_boundaries` walks the parent's blackbox
  instances and summarises each distinct `(module type, clock context)`
  exactly once (cached by that pair), so identical instances hit the
  cache — the full flattened graph is never materialised. The boundary
  map is now keyed **per instance**, so the same module type instantiated
  under two different clock domains gets a correct per-instance summary
  while identical instances still share one summarise call. It returns a
  `CompositionStats` record that *proves* the sharing (`cache_hits`,
  `summarised`, `declined`, `instances`, `boundary_modules`).
- **Boundary input-sink seeding restores flat-vs-abstracted parity**
  (#257). Data entering an opaque boundary at an input port in a domain
  foreign to the boundary's own clock is now reported: the summariser
  records the subtree's input/inout ports (in the boundary's `clock`
  domain) and `find_crossings` seeds a synthetic virtual *sink* flop at
  the boundary input pin, emitting a `Crossing` with the new
  `dst_boundary = (instance, port)` field (serialised in the JSON
  report). This mirrors the existing output-port virtual-source seeding,
  so abstracting a single-clock subtree with a foreign-domain input is
  now **result-preserving** — the previously over-conservative refusal to
  abstract such subtrees is retired. The `foreign_input_no_abstract`
  fixture now produces **identical** violations and `summary.*` counts
  between the flattened and the auto-abstracted runs (one CDC-004), while
  the abstracted run walks fewer flops. CDC-008 still never false-fires
  on a boundary clock pin (clock pins are excluded from input-sink
  seeding). The `shared_subtree_compose`, `single_clock_leaf_abstract`,
  and `foreign_input_no_abstract` fixture pairs pin the
  analyse-once / flat-vs-hierarchical parity properties.
- **Lint path auto-abstracts** (#257). The `lint` command now feeds the
  frontend's blackbox sibling modules into the same shared analysis core
  as `analyze` (`frontend.elaborate_with_blackboxes` /
  `frontends.yosys.elaborate_with_blackboxes`), so a `--blackbox` subtree
  is auto-abstracted on the lint path too. The `analyze` path stays
  frontend-free.

- **Auto-abstract single-clock subtrees** (#256, CDC-scaling epic #253
  phase 2). A blackboxed subtree whose entire clock set sits in one
  async-safe domain carries no internal crossing, so it is now
  automatically summarised to its port boundary and analysed as a
  boundary cell instead of walked flop-by-flop — the user no longer
  needs to know which subtrees are single-clock. New pure
  `abstract.is_single_clock_subtree` (the SDC-driven detector) and
  `abstract.summarise_subtree` (builds the P0 `BoundarySummary` from how
  the parent drives the instance's clock pin). `domain.find_crossings`
  gained a `boundaries=` argument: each summarised subtree's output port
  is re-seeded as a virtual source so a downstream sink in a foreign
  domain is still reported (the single `Crossing` type gains additive
  optional `src_boundary` / `dst_boundary` endpoint fields — the public
  JSON contract is unchanged). CDC-008 exempts blackbox boundary
  instances (both auto-abstracted and abstraction-declined) so a clock
  entering an opaque subtree isn't mis-flagged as clock-used-as-data.
  The orchestration (loading blackbox siblings, summarising, threading
  the boundary set) lives in `cli.py`; the frontend-free `analyze` path
  needs no new flag. Fixture pair `single_clock_leaf_abstract` proves the
  safety property — the flattened design and the auto-abstracted one
  produce identical violations and identical `summary.*`, with strictly
  fewer flops walked in the abstracted run. To keep abstraction
  result-preserving, the summariser **refuses to abstract** any subtree a
  foreign-domain or unconstrained signal is driven *into* (a data input):
  the output-only boundary seeds no input-side virtual sink yet (that is
  P3's `dst_boundary` work), so abstracting such a subtree would silently
  drop the crossing the flattened design reports at the subtree's first
  internal flop. New fixture `foreign_input_no_abstract` pins this down.

- **First-class blackbox boundary support** (#255, CDC-scaling epic #253
  phase 1). A large subtree can be excluded from flattening and analysed
  as a boundary cell so big integration blocks become tractable.
  `netlist.load` now accepts a flattened dump carrying blackbox boundary
  siblings — the single-module-after-flatten invariant is relaxed to
  "one top + N blackbox siblings", detected via the Yosys
  `attributes.blackbox` flag (no rename-to-`$` pass). New
  `netlist.load_with_blackboxes` returns `(top, dict[str, Module])`;
  `Module` gains optional `is_blackbox` / `boundary` fields (the
  `BoundarySummary` / `PortBoundary` schema is wired for the P2
  summariser). The yosys frontend threads a `blackbox: list[str]` into
  the `read_slang --blackboxed-module` line, surfaced as a repeatable
  `lint --blackbox MODULE` flag (requires the yosys-slang plugin). The
  pre-elaborated `analyze` path needs no new flag — a netlist already
  containing blackbox boundary modules loads transparently. Fixture pair
  `blackbox_leaf_crossing` exercises the boundary end to end. The
  analyzer-side consumption that reports a crossing *through* a blackbox
  (boundary-summary seeding) lands in phase 2 (#256).

- **slang-frontend defensive-path test sweep + coverage ratchet 90 → 93**
  (#252). New `tests/test_cov_slang_d.py` (74 cases) covers the slang
  frontend's conservative guards and best-effort constant-fold
  fallbacks — `_const_int` parameter / `ConstantValue` unwrapping,
  `_src_attr`'s source-range fallback chain, the `None`/typeless-input
  guards, and the `return None` legs of the expression/statement
  lowerers (unmodelled operands → `$_UNKNOWN_`, out-of-range / runtime
  selects, dead-arm case/if folds, dropped latches, for-loop steps that
  don't unroll). `frontends/slang.py` was the lone module under 90%
  (86%); it's now 95%, lifting the measured TOTAL to ~95%. The
  `--cov-fail-under` gate in the `pytest (with slang)` job is raised
  from 90 to 93 accordingly — ~2 points below the measured total, as
  drift headroom rather than a backfill target.

- **CI: end-to-end yosys-slang plugin (`read_slang`) oracle** (#251). A
  new `yosys-slang plugin` job in `test.yml` builds a pinned Yosys (v0.64,
  cached) from source and the rtl-buddy fork of `yosys-slang`
  (branch `rtl-buddy`, ccache-accelerated) into `build/slang.so`, then
  runs a gated test (`tests/test_yosys_slang_plugin.py`, marker
  `yosys_slang`) that elaborates a package-typed design through the
  `--yosys-plugin` / `RTL_BUDDY_SLANG_PLUGIN` path and asserts the
  expected CDC finding. The fixture
  (`tests/fixtures/slang_pkg_unsync_crossing`) imports a SystemVerilog
  package — a construct Yosys's built-in `read_verilog -sv` rejects — so
  a green result proves the plugin path is exercised, not bypassed.
  Every other job leaves the test skipped (`RTL_BUDDY_SLANG_PLUGIN`
  unset), so this is the first CI coverage of the `read_slang` branch.

- **`(* cdc_handshake *)` attribute — sanctioned four-phase req/ack
  handshake** (#247). A correct req/ack vector-CDC primitive (the
  `ip_cdc_handshake` shape) trips four rules on its protected paths —
  all false positives by protocol. Tag the source toggle, the held
  payload, and the destination capture register with
  `(* cdc_handshake *)` (alias `(* req_ack_handshake *)`) and the rule
  keyed at each is suppressed: CDC-013 on the toggle (backpressured
  until ack), CDC-020 on the payload (held stable across the req→ack
  window), CDC-001 on the capture (a single dst register is the intended
  capture under `dst_valid`), and CDC-014 on post-capture decode comb
  (ordinary datapath). Same attribute-on-netname convention as
  `(* cdc_sync *)` / `(* cdc_gray *)` / `(* cdc_static *)`; mark the
  blessed primitive once and every instance is recognised, retiring the
  per-instance waivers.

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

- **`--yosys-plugin` reads `RTL_BUDDY_SLANG_PLUGIN`**. The
  `lint --yosys-plugin` flag now falls back to the
  `RTL_BUDDY_SLANG_PLUGIN` environment variable when omitted (an
  explicit flag still wins). This lets the yosys-slang plugin
  location be a machine-local value instead of a hard-coded path:
  `rtl_buddy` already loads `.rtl-buddy/.env` into the environment
  before invoking `rtl-buddy-cdc`, so a config can simply drop the
  `--yosys-plugin` extra-arg and let the `.env` flow supply it.
  Mirrors `rtl_buddy`'s own slang `plugin-path` resolution.

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
through CDC-008 rule pack, `.swl`-style waiver matcher,
and text / JSON / SARIF reporters.

[Unreleased]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rtl-buddy/rtl-buddy-cdc/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rtl-buddy/rtl-buddy-cdc/releases/tag/v0.1.0
