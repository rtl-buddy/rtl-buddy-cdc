---
source_url: https://github.com/rtl-buddy/rtl-buddy-cdc/issues/107
ingested: 2026-05-19
---

# rtl-buddy-cdc reset-domain analysis

Reference for the reset-domain analysis pipeline implemented across
`src/rtl_buddy_cdc/reset_domain.py` and the RDC rule pack in
`src/rtl_buddy_cdc/rules.py` (issue #107). Companion to the emit-map
schema reference at
[`rtl-buddy-cdc-reset-domain-map-schema.md`](rtl-buddy-cdc-reset-domain-map-schema.md);
this document covers the *producers* of the data, that one covers
the JSON consumer contract.

## 1. Scope and non-goals

**Scope.** Classify every flop's reset pin to a structural source,
recognise reset-synchroniser chains, and emit the per-flop reset
crossings the RDC rule family reports on (RDC-001 through RDC-005).
The analysis runs on the same flattened `Module` as the clock-domain
pipeline; no new frontend work and no SDC dependency.

**Non-goals.**

- Not a power-domain / UPF analyzer. Reset polarity, type, and
  source classification only; nothing about retention, isolation,
  or always-on islands.
- Not a glitch / spike analyzer on the reset path. The rule pack
  reasons about *structural* reset crossings, not transient
  behaviour.
- Not a Tcl-driven reset spec. SDC has limited reset semantics;
  designer intent enters through SV attributes (and, when those
  aren't available, future YAML hints — explicitly deferred, see
  §8). No new SDC commands.

## 2. Data model

Three frozen dataclasses, all in `rtl_buddy_cdc.reset_domain`:

| Type | Field summary | Producer |
|---|---|---|
| `ResetSource` | `name`, `polarity` (`"high"`/`"low"`), `type` (`"sync"`/`"async"`), `source` (`"port"`/`"inferred"`/`"constant"`/`"comb"`), `clock` (`str | None`, see §7) | `assign_reset_domains` |
| `ResetDomain` | `flop` (cell name), `reset` (`ResetSource | None` — `None` for plain `$dff`/`$dffe`) | `assign_reset_domains` |
| `ResetCrossing` | `flop`, `reset`, `flop_clock` (`str | None`), `kind` (`"async-deassert"` / `"polarity-mismatch"` / `"sync-crossing"` / `"comb-driven"`) | `find_reset_crossings` |

`ResetSource.name` carries a different identifier per `source`:

- `"port"` — the top-level port name (`"rst_n"`).
- `"inferred"` — the driving flop's *cell name* (the one whose `Q`
  feeds this reset pin).
- `"constant"` — the literal bit-string (`"1'1"`, `"0"`, …).
- `"comb"` — empty string. The analyzer doesn't summarise
  combinational reset expressions in v1; consumers should treat
  this as "the reset is comb-driven; don't reason about its
  upstream".

Polarity on a `ResetSource` is the **flop pin**'s inferred polarity
(from the `$adff*` / `$sdff*` `*_POLARITY` parameter). It does *not*
reflect any user-level `(* reset_polarity *)` override; that
reconciliation is the consumer rule's job — see §6.

## 3. Cell-type → reset-pin map

`reset_domain.py` keys off two cell-type tables. Adding a new
reset-bearing flop type means extending one of them.

```python
_ASYNC_RESET_PINS = {
    "$adff":   ("ARST",  "ARST_POLARITY"),
    "$adffe":  ("ARST",  "ARST_POLARITY"),
    "$aldff":  ("ALOAD", "ALOAD_POLARITY"),
    "$aldffe": ("ALOAD", "ALOAD_POLARITY"),
    "$dffsr":  ("CLR",   "CLR_POLARITY"),
    "$dffsre": ("CLR",   "CLR_POLARITY"),
}
_SYNC_RESET_PINS = {
    "$sdff":   ("SRST", "SRST_POLARITY"),
    "$sdffe":  ("SRST", "SRST_POLARITY"),
    "$sdffce": ("SRST", "SRST_POLARITY"),
}
```

A few deliberate choices:

- **`$dffsr` surfaces `CLR`, not `SET`.** The SR-latch flop has
  independent `SET` and `CLR` async pins. `CLR` matches the
  dominant industry idiom (active-low clear); `SET` semantics
  differ enough that v1 punts. A future PR can add `SET`-aware
  handling without breaking the existing surface — extend the map,
  emit a second `ResetDomain` per cell, or attach the `SET` info to
  `ResetSource`. None of those are decided.
- **Polarity decode is conservative.** Yosys emits polarity as a
  multi-bit binary string (`"1"`, `"0"`, or the 32-bit padded form).
  `_polarity_from_param` reads only the trailing bit. Empty
  strings or unrecognised values default to `"low"` (the
  active-low idiom). See the function's docstring.

## 4. Source classification

`_classify_reset_source(module, bit, drivers)` is one hop deep —
the reset bit is matched against, in order:

1. **Constant** — non-int bit (Yosys constant tokens) → `"constant"`.
2. **Port** — the bit is a top-level input port's bit → `"port"`.
3. **Inferred** — the bit is the `Q` output of another flop →
   `"inferred"`, with that flop's cell name.
4. **Comb / untracked** — anything else (driven by a non-FF cell's
   output, or by a cell the analyzer doesn't recognise) → `"comb"`.

The walk is **deliberately shallow.** A multi-hop trace through
buffers / inverters lives elsewhere — the rule pack's
`_backward_fanin` does that lazily when it needs to. Keeping
`_classify_reset_source` to one hop means the data model stays
small and consumers know exactly what they're looking at; richer
shape recognition is layered on top, not baked into the source
field.

## 5. Reset-synchroniser recogniser

`find_reset_synchronizers(module, clock_domains, *, min_depth=2,
extra_synchronizers=None) → set[str]` returns the set of cell names
that participate in a recognised reset-synchroniser chain.

### 5.1 The pattern

The canonical reset-synchroniser is async-assert / sync-deassert:

```
                           ┌──── flop ───┐  ┌──── flop ───┐
   async reset ─── ARST ──►│ D       Q   ├─►│ D       Q   ├─► sync reset out
                           │   .---.     │  │   .---.     │
                       1'b1┤D─►│CLK│     │  │   │CLK│     │
                           └───┴─▲─┴─────┘  └───┴─▲─┴─────┘
                                 │                │
                              dest clock ─────────┘
```

`min_depth` flops on the destination clock, sharing the same async
reset source, chained Q→D, with the chain's head flop's `D` tied
to a constant (typically `1'b1` for active-low reset).

### 5.2 The load-bearing distinction

`_trace_reset_sync_chain` walks Q→D backward from the candidate
tail and returns a non-empty chain **only if** the walk terminates
at a constant `D`. Any of these fails the chain:

- A foreign-domain flop in the chain.
- A different reset source in the chain.
- A multi-bit `D` (chain head must be a single-bit flop).
- `D` driven by a port (not a constant, not another flop).
- Combinational logic in the chain (`D` driven by a non-FF cell).
- Depth exhaustion (`max_depth = min_depth + 4`).

Without the constant-fed-head check, any 2-flop chain sharing an
ARST in the same clock domain — including data-path register
chains that happen to share a reset — would false-positive as a
synchroniser. The constant `D` is what distinguishes "this chain
exists to synchronise the reset" from "this chain happens to be
ARST-shared".

### 5.3 The `extra_synchronizers` escape hatch

Some legitimate reset synchronisers don't have a constant-fed
head: chains whose head's `D` is fed by an upstream signal (e.g.
a power-good signal AND'd with the reset). The structural
recogniser deliberately refuses to match these — too many false
positives — but the user can override via
`(* reset_sync *)` / `(* reset_synchronizer *)` (see §8) and pass
the resulting flop set through `extra_synchronizers`. Members of
the override set are *added* to the recogniser's output, not
substituted.

## 6. find_reset_crossings — the unified emitter

`find_reset_crossings(module, clock_domains, *, recognised_syncs=None,
polarity_overrides=None) → list[ResetCrossing]` walks every flop
and emits one `ResetCrossing` per crossing-shape detected. Kinds:

| Kind | Triggered when |
|---|---|
| `comb-driven` | `reset.source == "comb"` — the reset pin is driven by combinational logic. |
| `async-deassert` | `reset.source == "inferred"` with `reset.type == "async"` and source flop's clock ≠ flop's clock. |
| `sync-crossing` | `reset.source == "inferred"` with `reset.type == "sync"` and source flop's clock ≠ flop's clock. |
| `polarity-mismatch` | `reset.source == "port"`, port has a `(* reset_polarity *)` declaration, and the declared polarity disagrees with the flop's inferred polarity. |

Two important properties:

- **Chain members are skipped explicitly.** A flop whose cell name
  is in `recognised_syncs` is filtered out at the top of the loop —
  the chain's head wires directly to the async reset, but emitting a
  crossing for it would be noise (the head is the synchroniser's
  whole job). Downstream consumers of the synced reset are filtered
  *implicitly*: their reset is `inferred` from the chain's tail flop,
  whose clock domain equals the consumer's clock domain, so the
  cross-domain check fails and no crossing fires.
- **`polarity-mismatch` is independent of crossing-kind.** A
  single flop with a port-declared polarity mismatch *and* a
  cross-domain async reset emits two `ResetCrossing` records.
  Consumers can dedupe on `flop` when they want a unique-flop
  view.

This function is additive — the per-rule walks in `check_rdc_00N`
still own their own classification. `find_reset_crossings` is the
public surface for *consumers* (downstream tooling, the
`--emit-reset-domain-map` artefact) that want the enumeration
without rerunning the whole rule pack.

## 7. The deferred `clock` field

`ResetSource.clock` is stubbed as `None` in v1.0. The intent is to
populate it with the clock that *samples* a sync reset — useful for
downstream tools rendering the reset graph, but it requires the
rule-side context (`_RuleContext.domains`) to be threaded through
`assign_reset_domains`, which would couple the foundation pass to
the rule pack's caching layer.

Filling the field is an additive change — `ResetSource` is frozen
but adding values to an optional field doesn't break callers, and
the emit-map schema already documents it as `null` until populated.
The PR that lands rule-side context plumbing is the natural place
to do this.

## 8. SV attributes

Two reset-flavoured user pragmas, both consumed by the rule pack
and by `find_reset_crossings`:

### 8.1 `(* reset_sync *)` / `(* reset_synchronizer *)`

Mark a flop as a vetted reset-synchroniser stage. Attached to the
wire/reg declaration the flop drives:

```sv
(* reset_sync *) logic rst_sync_q1;
(* reset_synchronizer *) logic rst_sync_q1;  // alias
```

Discovery via `rtl_buddy_cdc.rules.user_reset_sync_flop_names`.
Threaded into `find_reset_synchronizers` via the
`extra_synchronizers` parameter (§5.3). RDC-002 / RDC-004 / RDC-005
skip flops marked with this attribute and also skip consumers
whose ARST is driven by a marked flop's `Q` (the "this reset
arrives cleanly downstream" intent).

### 8.2 `(* reset_polarity = "low"|"high" *)`

Declare a **top-level reset port**'s active polarity as
authoritative. Attached to the input port:

```sv
(* reset_polarity = "low"  *) input logic rst_n;
(* reset_polarity = "high" *) input logic rst;
```

Discovery via `rtl_buddy_cdc.rules.user_reset_polarity_overrides`,
which returns `dict[port_name, "high"|"low"]`. Internal nets with
the attribute are silently ignored — the declaration is a
boundary-of-design intent, not a wire annotation.

RDC-002 grows a port-declared firing path: when a flop's async
reset traces back to a declared port and the flop's inferred
`ARST_POLARITY` disagrees with the declaration, RDC-002 fires with
a message naming the port and both polarities. This catches the
"designer wired a `posedge rst_n` flop on an active-low port" bug —
invisible to the inferred-source path (no producer flop to compare
against, no clock-domain crossing).

### 8.3 `--reset-hints FILE.yaml` (external hints)

For the "can't touch the source" cases (vendor IP, generated
wrappers, multi-block boards where one external file beats
scattered attributes) the same facts can be declared in an
external YAML file consumed via `--reset-hints` (issue #129):

```yaml
reset-hints:
  schema_version: "1.0"
  ports:
    - name: rst_n
      polarity: low
      type: async
  synchronizers:
    - instance_glob: "top.u_*.u_rst_sync_q[12]"
      role: reset_synchronizer
```

Opt-in via the `[hints]` install extra (`pip install
'rtl-buddy-cdc[hints]'`) — default installs stay `typer`-only;
PyYAML pulls in only when this extra is requested. Mirrors the
slang frontend's `[slang]`-extra pattern. Hints **win** on
disagreement with the SV-attribute path; synchroniser sets
union with no precedence question. Loader:
`rtl_buddy_cdc.reset_hints.load`. Field-by-field reference:
[`rtl-buddy-cdc-reset-hints-schema.md`](rtl-buddy-cdc-reset-hints-schema.md).

## 9. The RDC rule family

Implemented in `rules.py`, registered in `RULES`:

| Rule | Owns | Implementation |
|---|---|---|
| RDC-001 | Async reset crossing without a synchroniser | `check_rdc_001` — formerly `check_cdc_007`; renamed in #107, alias retained for back-compat waivers. |
| RDC-002 | Reset polarity mismatch | `check_rdc_002` — both the inferred-producer variant and the port-declared variant. |
| RDC-003 | Sync reset crossing a clock-domain boundary | `check_rdc_003` — SRST analogue of RDC-001's ARST walk. |
| RDC-004 | Reset driven by combinational logic | `check_rdc_004` — comb-driven ARST, scoped away from the multi-source case (RDC-005 owns that). |
| RDC-005 | Multiple reset sources converging without muxing | `check_rdc_005` — comb-of-ports shape RDC-004 deliberately skips. Severity `warning` (the AND-of-resets pattern is common enough that `error` would be too strong). |

Each rule consumes the same shared `_RuleContext` cache that the
CDC rule pack uses (built once per `run_all`). The reset-tree
walks reach into `assign_reset_domains` /
`find_reset_synchronizers` lazily — re-running them per rule on a
90-flop block is invisible; the rule pack's existing
context-caching pattern keeps the cost flat at higher fan-out.

**CDC-007 → RDC-001 alias.** The legacy `check_cdc_007` entry was
renamed at the rule-id level only; existing waivers keyed on
`CDC-007` continue to suppress findings via the alias map in
`waivers._LEGACY_RULE_ALIASES`. New waivers should key on
`RDC-001`.

## 10. Cross-references

- Architecture overview: [`rtl-buddy-cdc-architecture.md`](rtl-buddy-cdc-architecture.md)
- Emit-map artefact schema: [`rtl-buddy-cdc-reset-domain-map-schema.md`](rtl-buddy-cdc-reset-domain-map-schema.md)
- Producer module: `src/rtl_buddy_cdc/reset_domain.py`
- Rule pack: `src/rtl_buddy_cdc/rules.py` (RDC-001..-005 in the
  bottom half of the file; helpers `user_reset_sync_flop_names`
  and `user_reset_polarity_overrides` in the helpers block)
- Emit-map serializer: `src/rtl_buddy_cdc/reset_domain_map.py`
- Fixtures: `tests/fixtures/{bad,good}_rdc_*/`,
  `tests/fixtures/{bad,good}_marked_reset_*/`,
  `tests/fixtures/{good_reset_sync,marked_reset_sync}/`
