# CDC / RDC Lint Coverage Survey

A literature-and-tool survey of the CDC/RDC failure-mode taxonomy, mapped
against the rules currently shipped by `rtl-buddy-cdc`. The goal is to
identify which classes of bugs are not yet detected (or are only
partially detected) so they can be prioritised as future rules,
fixtures, or explicit "out-of-scope" sentinels.

The survey is built from open-literature sources and from first-principles
analysis of the analyzer's own structural detection. Principal references:

- **Cummings, C. "Clock Domain Crossing (CDC) Design & Verification
  Techniques Using SystemVerilog"** — SNUG 2008. The canonical
  industry reference for 2FF synchronisers, async FIFOs, handshake and
  pulse synchronisers.
- **Cummings, C. "Asynchronous & Synchronous Reset Design Techniques"**
  — SNUG 2002, plus the 2003 "Reset Architectures" follow-on. Source
  of the "async-assert, sync-deassert" reset pattern.
- **Litterick, M. "Pragmatic Simulation-Based Verification of Clock
  Domain Crossing"** — DVCon Europe 2006. Frames the limits of
  structural CDC analysis vs. functional protocol checking.
- The CDC/RDC failure-mode taxonomy as established in the open
  literature: metastability and 2FF chain depth, gating and gray
  encoding for multi-bit crossings, reconvergence, pulse-width
  preservation across slow-clock sampling, clock-network glitch
  hazards, and reset assertion/deassertion timing.

## 1. Current coverage

`rtl-buddy-cdc` ships 21 rules across two families:

**CDC (signal/data crossings):** CDC-001, -002, -003, -004, -005, -006,
-008, -009, -010, -011, -012, -013, -014, -015, -016. (CDC-007 was
renamed to RDC-001.)

**RDC (reset domain crossings):** RDC-001, -002, -003, -004, -005, -006.

Helper features that compose with rules:

- `(* cdc_sync *)`, `(* cdc_gray *)`, `(* cdc_static *)`,
  `(* reset_sync *)`, `(* reset_polarity *)` attributes for
  user-vetted shapes
- `set_input_delay` / `set_output_delay` port domain typing
- `set_clock_groups -asynchronous` / `-physically_exclusive`
- Structural Gray-code recognition (`g = b ^ (b >> 1)`)
- Reset-synchroniser chain recognition with constant-fed-head requirement
- Reconvergence phase-2 forward-cone filter

The only explicitly-unsupported pattern documented in the README is
**dual-port RAM crossings** (pinned by
`tests/fixtures/unsupported_dualport_ram_crossing/`).

## 2. Gap analysis

Each gap below is tagged by **impact** (how often it appears in
production silicon and how silent the failure is) and **detection
feasibility** (cheap structural check vs. requires new infrastructure).
Where a gap is bordered by an existing rule, the relationship is
called out so we don't add overlapping rules.

### 2.1 Sync-chain integrity — high-priority gaps

#### G-1. Latch in synchroniser chain (`$dlatch` as sync stage)

**Impact:** medium. Appears when `always_latch` or implicit latch
inference creeps into a sync path. Common in older code or in designs
that mix `always_comb` and `always_latch` carelessly. The failure is
silent: a transparent latch during the active phase of its enable does
not provide the full-cycle resolution time the chain depends on, and
MTBF collapses.

**Current behaviour:** `find_flops()` enumerates only `$dff*` cell
types (`src/rtl_buddy_cdc/flops.py:19`). A `$dlatch` between a source
flop and a destination flop is invisible to CDC-001 / CDC-002 /
CDC-014 — the chain walker doesn't see the latch as a stage and
doesn't see it as inter-stage combinational either.

**Proposed rule:** *CDC-017 — latch in CDC path.* Fires when a crossing's
destination-domain path contains a `$dlatch` / `$_DLATCH_*` cell
between the source flop's Q and the first dst-domain `$dff` stage.
Distinct from CDC-003 (comb only) and CDC-014 (comb between flop
stages).

**Detection feasibility:** cheap — single forward walk from the
destination flop's D backwards through dst-domain cells, stopping when
a flop or latch is hit.

#### G-2. Synchroniser fed by another synchroniser's tail (chained sync)

**Impact:** low-medium. Not a bug per se, but indicates a designer who
"double-synced just in case" or a refactor that left stale chains. Wastes
area and latency, and the *second* chain's source domain is now the
*first* chain's destination domain — a hidden re-classification.

**Current behaviour:** CDC-001/002 see each chain independently as
valid. No rule observes that the source flop of a chain is itself the
tail of another chain in the same destination domain.

**Proposed rule:** *CDC-018 — redundant cascaded synchroniser.* Severity
`warning`. Fires when a 2FF chain's source flop is itself the tail of
another 2FF chain in the same destination clock. Suppressed when the
intermediate flop's Q has a non-sync fanout (the first chain is doing
real work, the second is sampling its output).

**Detection feasibility:** cheap given `_sync_chain_flops`.

#### G-3. Pulse-synchroniser idiom recognition (false-positive on CDC-013)

**Impact:** high. The Cummings "pulse synchroniser" — toggle-FF in
source + 2FF + XOR at destination — is the canonical correct
solution to the fast-to-slow pulse problem. CDC-013 currently fires on
the *source-side* toggle flop because its `D = en ? ~Q : Q` shape
matches the bad-pattern classifier. Two unrelated questions get
conflated: (a) is the source a toggle? (b) is it part of a complete
pulse-synchroniser chain?

**Current behaviour:** CDC-013 fires on the toggle-FF shape regardless
of whether the destination chain implements the XOR-tail. A correct
pulse synchroniser silently violates CDC-013.

**Proposed fix:** extend CDC-013 with a positive-recognition phase. If
the source flop's Q crosses into the dst domain through a 2FF chain
*and* the chain's tail Q is XORed with the preceding stage's Q (or
fans out only to such an XOR), suppress. Pin with paired fixtures:
`bad_toggle_no_xor_tail` and `good_pulse_synchroniser`.

**Detection feasibility:** moderate — adds a tail-shape check to the
forward walk from the toggle flop's Q.

### 2.2 Multi-bit / bus crossings — medium-priority gaps

#### G-4. DMUX (1-of-N decode) crossing antipattern

**Impact:** high. A common antipattern: encode a state as one-hot in
the source domain, then sync each decoded bit independently. With
2FF-per-bit synchronisers, lanes resolve on different cycles and the
destination sees illegal intermediate one-hot codes ("01" then "11"
then "10" during a transition from "01" to "10").

**Current behaviour:** CDC-004 fires only when the source flop is
*itself* multi-bit and uncoded. If the source is N parallel 1-bit
flops in the same domain, each individually crossing through a 1-bit
sync chain, CDC-004 doesn't see a bus. CDC-005 catches reconvergence
*if* the decoded bits eventually meet at a single downstream flop, but
the structural disjoint-cone filter (issue #33) may let this through
when downstream lanes stay disjoint.

**Proposed rule:** *CDC-019 — independently-synced one-hot decode.* Fires
when N≥2 source-domain flops fan into N independent 2FF chains in the
same destination domain, the source flops share a common
combinational driver (one-hot of a common signal), and there is no
gating handshake or Gray encoding. Severity `error`.

**Detection feasibility:** moderate — requires a backward-fanin
intersection check across multiple sync chains' source flops.

#### G-5. Handshake protocol completeness (req without ack-back chain)

**Impact:** medium. CDC-012 already fires when a gated bus crossing
has no flop in the source domain whose D fans in from the destination
— a coarse "no back-handshake anywhere" check. The narrower failure:
a back-handshake exists, but the *ack* is not synchronised through a
2FF chain into the source domain. CDC-001/002 fire on the ack-domain
crossing independently, but only if the analyzer happens to find that
crossing — and the *missing* synchroniser-on-the-ack is a real bug
even when no flop in src domain has D fanin from dst at all.

**Current behaviour:** if src→dst gated bus exists but no dst→src
back-handshake exists *at all*, CDC-012 fires. If a back-handshake
exists but is broken (the ack is unsynchronised), CDC-001 fires on it,
but the user sees a generic "unsynchronized crossing" finding rather
than "handshake ack is unsynchronised."

**Proposed enhancement:** when CDC-001/002 fires on a crossing whose
destination flop is a `$dffe.EN` feeding a `$mux.S` that itself feeds
a multi-bit data crossing's destination, retag or annotate the
violation as "handshake ack synchroniser missing" — surfacing the
shape relationship to the user.

**Detection feasibility:** cheap — a forward-fanout pattern match in
the reporter on already-detected CDC-001 findings.

#### G-6. Bus reconvergence between independently-synced lanes

**Impact:** medium. Multi-bit bus sliced into N 1-bit independently
synced lanes, then *recombined* at the destination into a multi-bit
register or comparator. Each lane has different metastability skew, so
the recombined value can transit through invalid intermediate codes
before settling. Distinct from G-4 (one-hot) — the source is genuinely
multi-bit, just sliced for convenience.

**Current behaviour:** CDC-004 only sees the bus at the destination
flop's `D` pin; if the destination is per-bit flops, the bus shape is
hidden. CDC-005 catches reconvergence when downstream cone is shared.

**Proposed rule:** *CDC-020 — sliced bus crossing.* Fires when N source
flops in the same domain share a common name prefix (e.g.
`payload_q[0..7]`) and cross through N independent 2FF chains, then
reconverge into a common multi-bit destination register or
arithmetic/compare cell. Severity `error`.

**Detection feasibility:** moderate — needs grouping by source flop
naming or by source-cell parameter relationships.

### 2.3 Reset domain — medium-priority gaps

#### G-7. Async-assert / sync-deassert pattern enforcement

**Impact:** high. The canonical Cummings reset-synchroniser asserts
asynchronously (so logic enters reset immediately) but deasserts
synchronously (so the release edge meets recovery/removal against the
destination clock). Today's reset-sync recognizer
(`find_reset_synchronizers`) requires a constant-fed head, but does
not verify the head's `D` is the *deasserted* polarity — i.e. for an
active-low reset, the head's `D` must be tied to `1` (constant `1`),
not `0`.

**Current behaviour:** the recogniser accepts any constant-fed chain
head. A reset sync chain whose head is tied to the *asserted* polarity
is mis-recognised as a valid synchroniser when it is in fact a one-shot
that never deasserts.

**Proposed rule:** *RDC-007 — reset synchroniser deassertion polarity.*
Fires when a flop is recognised as a reset-sync-chain head and its
constant `D` matches the *asserted* polarity for its consumers'
`ARST_POLARITY`. Severity `error`.

**Detection feasibility:** cheap — the recogniser already knows the
constant value; cross-check against consumer polarity.

#### G-8. Recovery / removal on async reset deassertion edges

**Impact:** medium. Even with a proper reset synchroniser, an async
reset whose *deassertion* edge is structurally unsynchronised to the
destination clock (e.g. raw reset port directly tied to a flop's
`ARST` with no synchroniser) violates recovery/removal timing.

**Current behaviour:** RDC-001 / RDC-003 catch the "crossing from a
foreign clock domain" case. They do *not* catch the "asynchronous
top-level reset port that's also declared a reset-only port (no clock
declared)" case, where the reset is *intentionally* asynchronous to
every clock but its deassertion still needs sync.

**Proposed rule:** *RDC-008 — unsynced primary-reset port deassertion.*
Fires when a top-level port marked via `(* reset_polarity *)` (or
inferred as a reset port by name pattern + lack of clock domain)
directly drives one or more flops' `ARST` without a recognised reset
synchroniser in any consumer clock domain.

**Detection feasibility:** moderate — needs the recogniser to walk
from each reset-typed port forward to flops.

#### G-9. Glitchless clock-mux structural pattern

**Impact:** medium-high. The standard glitchless clock mux uses two
cross-coupled latches gated by the inverted clocks, so the
deassertion of one input and the assertion of the other are
separated by at least half a cycle on each clock. CDC-010 catches the
*bad* case (raw `$mux` selecting between clocks with a wrong-domain
select). It doesn't *recognise* the glitchless template — if a user
builds the textbook glitchless mux out of `$dlatch` cells and CDC-010
fires on the select, the recommended fix ("synchronise the select")
would actually break it.

**Proposed enhancement:** add structural recognition of the canonical
glitchless-mux shape (two cross-coupled `$dlatch` cells, opposite
clock polarities, output `$and`-gated with each clock). Suppress
CDC-010 when the mux's select reaches one of those latches'
combinational inputs.

**Detection feasibility:** moderate — distinctive shape but several
small variants exist in practice; risk of either-direction false
positives.

### 2.4 Clock-domain definition — lower-priority gaps

#### G-10. Flop CLK driven by an undeclared port

**Impact:** medium. Failure mode: the SDC author forgets `create_clock`
on a clock port. Today, the analyzer assigns the flop to a synthetic
domain rooted at the port. Two flops driven by the same port get the
same synthetic domain — they pass CDC-001 trivially even though the
port might have been intended as a different clock from neighbouring
declared clocks.

**Current behaviour:** flops with an undeclared CLK port get a
synthetic domain. Crossings *to/from* those flops show up correctly
when the other side is in a different domain, but two undeclared
CLK ports clocking related flops collapse into the same synthetic
domain only if they're the same net.

**Proposed rule:** *CDC-021 — flop clocked by undeclared port.*
Severity `warning`. Fires when a flop's CLK is driven by a top-level
input port that has no `create_clock` declaration in the SDC.
Complements CDC-011 (unconstrained primary input on data pin).

**Detection feasibility:** cheap.

#### G-11. Conflicting `create_clock` / `create_generated_clock` on same net

**Impact:** low. Symptom: two `create_clock` calls on the same net, or
a `create_generated_clock` whose master resolves to multiple distinct
roots. The current SDC parser silently picks one.

**Proposed enhancement:** add an SDC linter pass that emits a warning
when the clock graph has structural conflicts (multiple roots,
cycles, generated clocks with missing masters). Lives in
`rtl_buddy_cdc.sdc`, not `rules.py`.

**Detection feasibility:** cheap.

### 2.5 Gate-level / tech-mapped netlists — coverage limit

#### G-12. Gate-level `$_DFF_*` flops invisible to `find_flops()`

**Impact:** high *if users run CDC after `abc`*; low if they don't.
`find_flops()` enumerates only higher-level Yosys cell types
(`$dff`, `$dffe`, etc.). A netlist that's been through `abc` is
populated with `$_DFF_P_` / `$_DFF_N_` / `$_DFFE_*` / `$_SDFFE_*`
cells that the function ignores — silently, no warning. The analyzer
returns "no flops found, no crossings" on tech-mapped netlists.

**Current behaviour:** CDC-016 has its own gate-level cell-type
knowledge for the polarity check, so it works on chains *if* the
chains were found — but the chains come from `find_flops()`, so on
a gate-level netlist no chain is ever found.

**Proposed fix:** extend `FF_CELL_TYPES` to include the gate-level
families, or add a parallel `find_flops_gatelevel()` that the
analyzer composes with for tech-mapped inputs. Equivalently: emit a
loud warning when the netlist contains zero higher-level FF cells
but non-zero `$_DFF_*` cells.

**Detection feasibility:** cheap — the cell types are well-known.

### 2.6 Synchronisation protocol — out of scope for structural lint

These are out of scope for `rtl-buddy-cdc` and are listed only so the
roadmap doesn't accidentally adopt them:

- **MTBF / soft-error modelling.** Requires technology-specific
  metastability resolution time τ; structural lint can't compute it.
- **Functional handshake protocol verification.** "Req stays high
  until ack returns" requires temporal logic / formal — out of scope
  for a structural lint.
- **Floor-plan-aware synchroniser placement.** The "place sync at
  domain boundary" recommendation is physical, not structural.
- **Power-domain / UPF isolation interactions.** Needs UPF input.

### 2.7 Already covered or correctly out-of-scope

Verified against the implementation, not gaps:

- **Sync chain stages on different clocks.** `_sync_chain_depth` enforces
  `domains.get(nxt.cell.name) == head_clock` at every step
  (`src/rtl_buddy_cdc/rules.py:655`). Mixed-clock stages cause the
  chain to terminate at the boundary and CDC-001 fires naturally.
- **Reset sync chain constant-fed-head check.**
  `_trace_reset_sync_chain` requires a constant `D` at the head
  (`src/rtl_buddy_cdc/reset_domain.py:316`); chains with port- or
  flop-fed heads are correctly rejected.
- **Dual-port RAM crossings.** Pinned as unsupported by
  `tests/fixtures/unsupported_dualport_ram_crossing/`.

## 3. Prioritised roadmap

Ordered by **(impact × prevalence) / (implementation cost)**:

1. **G-12** — gate-level `$_DFF_*` coverage (or loud warning). Highest
   priority because it's a silent zero-finding mode on common
   netlist shapes.
2. **G-3** — pulse-synchroniser positive recognition. Removes a
   credible false positive on a canonical correct idiom.
3. **G-1** — latch-in-sync-chain rule (CDC-017). Silent MTBF
   collapse; cheap to detect.
4. **G-7** — async-assert / sync-deassert deassertion-polarity check
   (RDC-007). The reset-sync recogniser already has the data.
5. **G-4** — independently-synced one-hot decode (CDC-019). High
   impact, moderate cost.
6. **G-10** — flop CLK on undeclared port (CDC-021). Cheap, pairs
   naturally with CDC-011.
7. **G-9** — glitchless clock-mux recognition. High value but harder
   to make precise without false negatives.
8. **G-6** — sliced-bus reconvergence (CDC-020). Medium value, needs
   naming/grouping heuristic.
9. **G-2** — cascaded synchroniser warning (CDC-018). Quality-of-life.
10. **G-5** — handshake ack-missing tagging. Reporter-only refinement
    on existing findings.
11. **G-8** — primary-reset-port unsynced deassertion (RDC-008).
12. **G-11** — SDC clock-graph linter.

Each item is suitable for a single GitHub issue + paired fixture PR
following the existing convention (paired `bad_*` / `good_*` fixtures
with `.sv` / `.sdc` / `.json` and auto-generated `README.md`).
