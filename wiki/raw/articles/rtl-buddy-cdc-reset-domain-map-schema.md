---
source_url: https://github.com/rtl-buddy/rtl-buddy-cdc/issues/108
ingested: 2026-05-18
---

# rtl-buddy-cdc reset-domain-map schema (v1.0)

Reference for the JSON artefact emitted by
`rtl-buddy-cdc --emit-reset-domain-map FILE.json`. Parallel to the
clock-domain-map (`--emit-domain-map`); both can be passed in a
single invocation.

Producer: `rtl_buddy_cdc.reset_domain_map.build_reset_domain_map`.
Schema version constant:
`rtl_buddy_cdc.reset_domain_map.SCHEMA_VERSION` (`"1.0"`).

## Stability contract

- Renaming or retyping any documented field is a breaking change and
  requires bumping `schema_version`.
- Adding new keys (top-level or nested) is backward-compatible.
- Every collection is sorted by a documented key; two builds on the
  same inputs produce the same byte sequence. Pinned by
  `tests/test_reset_domain_map.py::test_deterministic`.
- The contract is enforced in CI by
  `tests/test_reset_domain_map.py` — golden-file diff plus
  type/key/ordering assertions.

## Envelope

```json
{
  "schema_version": "1.0",
  "generator": {"name": "rtl-buddy-cdc", "version": "0.2.0"},
  "design":    {"top": "<top_module>", "frontend": "yosys"},
  "reset_sources":        [ ... ],
  "reset_synchronizers":  [ ... ],
  "flop_resets":          [ ... ],
  "reset_crossings":      [ ... ]
}
```

The `generator` and `design` blocks match the clock-domain map's
envelope so a consumer joining the two artefacts can rely on
`design.top` being identical when both are emitted in one run.

## `reset_sources`

One entry per distinct upstream reset observed across the design,
keyed by `(name, source)`, over every reset pin: each flop's reset (each
lane reset of a register whose lanes are reset differently, since
rtl-buddy-cdc#398) and every asynchronous control (a set / clear flop's
`SET` too, since rtl-buddy-cdc#390). Sorted by `(source, name)`.

| Field | Type | Description |
|---|---|---|
| `name` | string | Port name (`source="port"`), driver flop cell name (`source="inferred"`), or constant literal (`source="constant"`). |
| `source` | enum | One of `"port"`, `"inferred"`, `"constant"`. (`"comb"` sources are intentionally omitted — they're surfaced in `reset_crossings` instead.) |
| `polarity` | enum | `"high"` or `"low"`. For port sources with a `(* reset_polarity *)` declaration this is the **declared** polarity; otherwise it's the polarity the first observed consumer flop infers. |
| `type` | enum | `"sync"` or `"async"`. |
| `clock` | string \| null | The clock that samples a sync reset. **v1.0 always emits `null`** — population is gated on the rule-side reset-context PR (see `ResetSource` docstring). |
| `via_synchronizer` | bool | `true` when `source == "inferred"` and the driver flop is in the recognised reset-synchroniser set. |
| `declared_polarity` | enum (optional) | Present only when `source == "port"` and the port carries a `(* reset_polarity *)` attribute; the declared value. Disagreement with a consumer's inferred polarity surfaces as a `polarity-mismatch` crossing. |
| `location` | object (optional) | Best-effort source location. Format matches `flop_resets[].location` below. |

## `reset_synchronizers`

One entry per flop in the recognised reset-synchroniser set (union of
`find_reset_synchronizers`'s structural matches and user
`(* reset_sync *)`-marked flops). Sorted by `instance_path`.

| Field | Type | Description |
|---|---|---|
| `instance_path` | string | `<top>.<parents>.<leaf>` for the sync-stage flop. |
| `dest_clock` | string \| null | The clock domain that samples the synchroniser. |
| `async_in` | string (optional) | Upstream reset source name (the asserted reset that this chain synchronises). |
| `async_in_kind` | string (optional) | `source` of that upstream reset (`"port"` / `"inferred"` / `"constant"`). |
| `location` | object (optional) | Source location of the sync-stage cell. |

A chain view (head, tail, depth) is not in v1.0 — the producer
returns a flat set of member cells. Adding chain identity is a
backward-compatible v1.x extension if a consumer needs it.

## `flop_resets`

One entry per flop with a reset (flops without one are omitted — this
section is the reset inventory, not the flop inventory), or one per
reset for a register whose lanes are reset differently (below). Sorted
by `(instance_path, lanes)`. A reset is a reset pin, or, on a flop
without one, a synchronous reset that the `proc` netlist form leaves as
constant-leg muxes on `D` where `opt_dff` would fold it into `SRST`
(rtl-buddy-cdc#394). Several selects classify as `"comb"`, as the cell
`opt_dff` adds to combine them does: `$reduce_and`, active low, when
every select is active low, else `$reduce_or`, active high. An `$sdff*`
whose `D` still carries such a mux — the slang frontend folds the first
select of `if (a) … else if (b)` into `SRST` and leaves the second on
`D` — reads as both selects, where `opt_dff` would fold the mux into
that pin: not under the enable of a reset-over-enable `$sdffe`, and
only on lanes whose constant is the `SRST_VALUE` bit (rtl-buddy-cdc#398).

A register whose lanes have different resets is sliced by `opt_dff`
into one cell per reset (and one with none, for lanes never reset). The
`proc` form's one cell then has one entry per reset, each with the
`lanes` it resets (rtl-buddy-cdc#398), so the `proc` and `opt_dff`
forms of one design, and the Yosys and slang frontends, list the same
resets. Known residual: slang writes `else if (!b)` as an active-high
mux behind a `$logic_not` where `proc` swaps the mux legs, so a
combination of active-low selects reads `"comb"` active high on slang
and active low on Yosys.

| Field | Type | Description |
|---|---|---|
| `instance_path` | string | `<top>.<parents>.<leaf>` for the flop. |
| `clock` | string \| null | Flop's clock domain (matches `domain-map.flop_domains[].clock`). |
| `reset` | string | Upstream reset's `name` (joins to `reset_sources[].name`). |
| `reset_kind` | enum | `source` of that reset (`"port"` / `"inferred"` / `"constant"` / `"comb"`). |
| `polarity` | enum | Polarity the reset signal acts with (`"high"` / `"low"`): the flop pin's, except where Yosys's encoding of the pin inverts it — an active-low clear / set of a set / clear flop, written by `proc` as an inverter in front of an active-high `CLR` / `SET` (rtl-buddy-cdc#394). |
| `type` | enum | `"sync"` or `"async"`. |
| `lanes` | array of int (optional) | Present only on the entries of a register whose lanes are reset differently: the lanes (bit offsets into the flop's `Q`, ascending) this reset acts on. Absent: the reset acts on the whole flop. Added in rtl-buddy-cdc#398 (additive). |
| `location` | object (optional) | Yosys-derived source location: `{file, start_line, start_column, end_line, end_column}` — column/end fields present when Yosys emitted them. |

## `reset_crossings`

Structural reset crossings the RDC rule pack would flag, one per reset
pin: a set / clear flop whose `CLR` and `SET` both cross has two
entries (rtl-buddy-cdc#390), a register whose lanes are reset
differently one per lane reset (rtl-buddy-cdc#398). Each kind reads the
pins its rule reads: `async-deassert` and `comb-driven` every
asynchronous control, `sync-crossing` the `SRST` pin (each lane reset),
`polarity-mismatch` the flop's reset (`flop_resets[].reset`, each of
them). Sorted by `(instance_path, kind, pin, lanes)`.

| Field | Type | Description |
|---|---|---|
| `instance_path` | string | Destination flop. |
| `kind` | enum | One of `"async-deassert"` (RDC-001 shape), `"polarity-mismatch"` (RDC-002 port-declared variant), `"sync-crossing"` (RDC-003), `"comb-driven"` (RDC-004). |
| `flop_clock` | string \| null | The flop's clock domain. |
| `reset` | string | Upstream reset name. |
| `reset_kind` | enum | `source` of that reset. |
| `polarity` | enum | The polarity the reset acts with on that pin (as `flop_resets[].polarity`). |
| `type` | enum | `"sync"` or `"async"`. |
| `pin` | string | The flop pin the reset arrives on, by its coarse name: `"ARST"`, `"ALOAD"`, `"CLR"`, `"SET"` or `"SRST"` (gate-level `R` / `L` / `S` read as the pin they stand for). Added in rtl-buddy-cdc#390 (additive). |
| `lanes` | array of int (optional) | As `flop_resets[].lanes`: present only on the record of a reset that acts on some lanes of the flop. Added in rtl-buddy-cdc#398 (additive). |
| `location` | object (optional) | Flop's source location. |

Rule `severity`, waiver suppression, and the user-facing message
text are intentionally not emitted here. Those live in the normal
findings report (`--format json|sarif|text`); the map is the
structural truth.

## Composition with the clock-domain map

Both flags accept independent output paths and can be passed in a
single invocation:

```bash
rtl-buddy-cdc analyze \
    --netlist out.json --sdc design.sdc \
    --emit-domain-map clock-map.json \
    --emit-reset-domain-map reset-map.json
```

Consumers cross-referencing the two artefacts can join
`flop_resets[].instance_path` to `flop_domains[].instance_path` and
`reset_crossings[].instance_path` to `crossings[].dst_flop`.

## Diffability and CI use

Because every collection is sorted deterministically, the artefact
can be committed as a golden file and diffed in code review — the
fixture `tests/fixtures/bad_marked_reset_polarity/` does exactly
that. To regenerate after an intentional change, from the repo root:

```bash
uv run rtl-buddy-cdc analyze \
    --netlist tests/fixtures/<fix>/<fix>.json \
    --sdc tests/fixtures/<fix>/<fix>.sdc \
    --project-root . \
    --emit-reset-domain-map tests/fixtures/<fix>/<fix>.reset-domain-map.json \
    --no-findings
```

`--project-root .` matters: a relative `--emit-*` path is anchored at
`--project-root`, else at the SDC's directory (rtl-buddy-cdc#245), so
without it the file lands under `tests/fixtures/<fix>/tests/...`.
`--no-findings` skips rule evaluation; combine with one or both
`--emit-*-map` flags when the map is the sole deliverable of the
run.
