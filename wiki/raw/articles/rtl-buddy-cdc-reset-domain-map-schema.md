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
keyed by `(name, source)`. Sorted by `(source, name)`.

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

One entry per flop with a reset pin (plain `$dff` / `$dffe` are
omitted — this section is the reset inventory, not the flop
inventory). Sorted by `instance_path`.

| Field | Type | Description |
|---|---|---|
| `instance_path` | string | `<top>.<parents>.<leaf>` for the flop. |
| `clock` | string \| null | Flop's clock domain (matches `domain-map.flop_domains[].clock`). |
| `reset` | string | Upstream reset's `name` (joins to `reset_sources[].name`). |
| `reset_kind` | enum | `source` of that reset (`"port"` / `"inferred"` / `"constant"` / `"comb"`). |
| `polarity` | enum | Flop pin's inferred polarity (`"high"` / `"low"`). |
| `type` | enum | `"sync"` or `"async"`. |
| `location` | object (optional) | Yosys-derived source location: `{file, start_line, start_column, end_line, end_column}` — column/end fields present when Yosys emitted them. |

## `reset_crossings`

Structural reset crossings the RDC rule pack would flag. Sorted by
`(instance_path, kind)`.

| Field | Type | Description |
|---|---|---|
| `instance_path` | string | Destination flop. |
| `kind` | enum | One of `"async-deassert"` (RDC-001 shape), `"polarity-mismatch"` (RDC-002 port-declared variant), `"sync-crossing"` (RDC-003), `"comb-driven"` (RDC-004). |
| `flop_clock` | string \| null | The flop's clock domain. |
| `reset` | string | Upstream reset name. |
| `reset_kind` | enum | `source` of that reset. |
| `polarity` | enum | The flop's inferred reset-pin polarity. |
| `type` | enum | `"sync"` or `"async"`. |
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
that. To regenerate after an intentional change:

```bash
uv run rtl-buddy-cdc analyze \
    --netlist tests/fixtures/<fix>/<fix>.json \
    --sdc tests/fixtures/<fix>/<fix>.sdc \
    --emit-reset-domain-map tests/fixtures/<fix>/<fix>.reset-domain-map.json \
    --no-findings
```

`--no-findings` skips rule evaluation; combine with one or both
`--emit-*-map` flags when the map is the sole deliverable of the
run.
