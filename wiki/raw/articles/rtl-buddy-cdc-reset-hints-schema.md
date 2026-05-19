---
source_url: https://github.com/rtl-buddy/rtl-buddy-cdc/issues/129
ingested: 2026-05-19
---

# rtl-buddy-cdc reset-hints schema (v1.0)

Reference for the external YAML file consumed by
`rtl-buddy-cdc --reset-hints FILE.yaml`. Parallel to the in-RTL
`(* reset_polarity *)` / `(* reset_sync *)` SV attributes
(`wiki/raw/articles/rtl-buddy-cdc-reset-domain-analysis.md` §8) —
same vocabulary, external file when the user can't touch RTL.

Loader: `rtl_buddy_cdc.reset_hints.load`. Schema version constant:
`rtl_buddy_cdc.reset_hints.SCHEMA_VERSION` (`"1.0"`). Opt-in via
the `[hints]` install extra (`pip install 'rtl-buddy-cdc[hints]'`);
default installs stay `typer`-only — PyYAML is the only thing the
extra pulls in.

## Stability contract

- Renaming or retyping any documented field is a breaking change
  and requires bumping `schema_version`.
- Adding new keys (top-level or nested) is backward-compatible.
- Strict parsing: typos, unknown keys, malformed enum values fail
  with file:line context. The loader is the *one* place this stays
  loud — the analyzer's tolerant decoders only apply to in-RTL
  attributes the user might have copy-pasted in the wild.

## Envelope

```yaml
reset-hints:
  schema_version: "1.0"   # optional; defaults to the producer's version
  ports:           [ ... ]
  synchronizers:   [ ... ]
```

The wrapping `reset-hints:` key is required so the file can grow
sibling sections later (`clock-hints:`, `power-hints:`) without
breaking back-compat. Both `ports:` and `synchronizers:` are
optional — emit only what you have.

## `ports`

Port-level reset declaration. Parallel to
`(* reset_polarity = "low"|"high" *)` on a top-level reset port.

```yaml
ports:
  - name: rst_n
    polarity: low      # required: "low" | "high"
    type: async        # optional: "async" (default) | "sync"
    clock: clk_b       # optional; only meaningful when type: sync
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Top-level reset port name. |
| `polarity` | enum | yes | `"low"` = active-low (asserts on `0`, the classic `rst_n` idiom); `"high"` = active-high. Same convention as `ResetSource.polarity` and the `(* reset_polarity *)` attribute. |
| `type` | enum | no | `"async"` (default) or `"sync"`. |
| `clock` | string | no | The clock that samples a sync reset. Reserved for the future `ResetSource.clock` population (currently stubbed as `null` everywhere; see analysis doc §7). |

A hint targeting a port that doesn't exist on the module is
**silently dropped** at the consumer-side merge. The loader's
strict validation only checks the file is well-formed; whether
each `name` resolves to a real port is a runtime concern the rule
pack handles by ignoring unknown ports. Rationale: the SV
attribute path is also tolerant of dangling annotations, and a
hints file shared across multiple sub-blocks shouldn't need
per-block trimming.

## `synchronizers`

Mark a flop cell (or set of cells) as a vetted reset-synchroniser
stage. Parallel to `(* reset_sync *)` / `(* reset_synchronizer *)`
on a netname driven by a flop's `Q`.

```yaml
synchronizers:
  - instance: top.u_rstgen.u_sync_q1   # exact pre-flatten dotted path
  - instance_glob: "top.u_*.u_rst_sync_q[12]"   # shell glob
    role: reset_synchronizer
```

| Field | Type | Required | Description |
|---|---|---|---|
| `instance` | string | one of two | Exact pre-flatten dotted path (`<top>.<parents>.<leaf>`). |
| `instance_glob` | string | one of two | Shell-style glob (`*`, `?`, `[…]`) matched against the same resolved path. `fnmatch.fnmatchcase` semantics. |
| `role` | string | no | Tag for future expansion (`reset_generator`, …). v1 only accepts `"reset_synchronizer"`; other roles raise. |

Exactly one of `instance` or `instance_glob` must be set; the
loader rejects entries with both or neither. Glob matching is
against the *resolved* hierarchical path, not the raw post-flatten
cell name — so `top.u_rstgen.u_sync_q1` matches the same flop
whether Yosys gave it a `$flatten\\…` prefix or the slang frontend
left it dotted. The resolver is
`rtl_buddy_cdc.domain_map._hier_path`.

## Precedence

When an SV attribute and a YAML hint disagree on the same port's
polarity, **the hint wins**. The hints file is the explicit
override; the attribute is the in-RTL default. The disagreement
isn't logged today; if a user request surfaces, add a
`--verbose`-gated note at the merge site.

Synchroniser sets *union* with no precedence question — both sides
mark the same kind of fact (this cell is a sync stage). The merged
set is what `find_reset_synchronizers` accepts via
`extra_synchronizers`.

## CLI

```bash
# Default analyzer flow + hints
rtl-buddy-cdc analyze \
    --netlist out.json --sdc design.sdc \
    --reset-hints hints.yaml

# Combine with emit-map artefacts
rtl-buddy-cdc analyze \
    --netlist out.json --sdc design.sdc \
    --reset-hints hints.yaml \
    --emit-reset-domain-map reset-map.json
```

The `lint` subcommand accepts the flag identically. When the
`[hints]` extra is missing, both subcommands exit 2 with a
`pip install 'rtl-buddy-cdc[hints]'` message — mirrors the slang
frontend's `SlangFrontendUnavailable` contract.

## Worked example

A module with no SV attributes, plus an external file that drives
the same polarity-mismatch finding the attribute path would
produce:

```sv
// designs/top.sv
module top (
    input  logic clk,
    input  logic rst_n,        // no (* reset_polarity *)
    input  logic d_in,
    output logic q_out
);
    logic bad_q;
    // posedge on rst_n — Yosys infers ARST_POLARITY=1
    always_ff @(posedge clk or posedge rst_n)
        if (rst_n) bad_q <= 1'b0;
        else       bad_q <= d_in;
    assign q_out = bad_q;
endmodule
```

```yaml
# designs/top.hints.yaml
reset-hints:
  schema_version: "1.0"
  ports:
    - name: rst_n
      polarity: low
      type: async
```

```bash
rtl-buddy-cdc lint --top top --sdc designs/top.sdc \
                   --reset-hints designs/top.hints.yaml \
                   designs/top.sv
# RDC-002: reset polarity mismatch with port-level declaration on rst_n
```

Mirrored exactly by the test fixture
`tests/fixtures/bad_hints_reset_polarity/` —
`bad_hints_reset_polarity.{sv,sdc,hints.yaml,json}`.

## Cross-reference

- Analysis-side pipeline: [`rtl-buddy-cdc-reset-domain-analysis.md`](rtl-buddy-cdc-reset-domain-analysis.md) §8
  (SV attributes — same vocabulary, different surface).
- Emit-map schema (consumer-facing): [`rtl-buddy-cdc-reset-domain-map-schema.md`](rtl-buddy-cdc-reset-domain-map-schema.md).
- Migration utility from Spyglass SGDC: rtl-buddy-cdc#131
  (blocked on this issue / not yet implemented).
- Loader module: `src/rtl_buddy_cdc/reset_hints.py`.
- CLI wiring: `src/rtl_buddy_cdc/cli.py::_RESET_HINTS_OPT`.
