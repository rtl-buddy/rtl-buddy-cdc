---
title: CDC Data Model
created: 2026-05-11
updated: 2026-05-14
type: concept
tags: [data-model, netlist, cdc, flops, clock-domain, crossing, sdc, frontend]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# CDC Data Model

The analyzer is structured as a sequence of pure functions producing immutable dataclasses. Frozen dataclasses are used everywhere except `ClockSpec`'s parser-built collections.

The `Module` shape described here is the **contract every [[elaboration-frontends|frontend]] must produce**. Whether `Module` comes from Yosys `write_json` or from a pyslang elaboration, the downstream pipeline sees the same dataclasses with the same conventions.

## Netlist Layer

A `Module` is a flat namespace of `ports`, `cells`, and `netnames`, each indexed by name.

**`Bit`** — the fundamental connection type. Either an `int` (net ID, originating from Yosys IDs or from the slang frontend's sequential allocator) or one of the constant strings `"0"`, `"1"`, `"x"`, `"z"`. The integer-vs-string distinction is load-bearing — every walker ignores constant bits because they can't propagate driver identity.

**`Netname`** — the wire/reg name as it appeared in the source, with any SV `(* attr = "value" *)` annotations attached. Yosys preserves attributes on the netname rather than the driving cell (and the slang frontend matches this convention by pulling attributes via `Compilation.getAttributes(symbol)`), so the rule pack maps from tagged netname bits back to the upstream flop uniformly.

## Flop Recognition

`FF_CELL_TYPES` enumerates the 11 Yosys FF variants: `$dff`, `$dffe`, `$adff`, `$adffe`, `$aldff`, `$aldffe`, `$sdff`, `$sdffe`, `$sdffce`, `$dffsr`, `$dffsre`. Each has different reset/enable plumbing, but **CLK / D / Q are universal** — the only pins needed for domain assignment and fanout walks. The slang frontend emits the subset that corresponds to recognised `always_ff` shapes (`$dff` for plain `posedge clk`, `$adff` for the canonical `always_ff @(posedge clk or negedge rst_n)` async-reset shape).

## Domain Assignment

`FlopDomain` ties a `Flop` to a `clock: str | None` — the top-level input port name driving its CLK, or `None` if unresolved within the trace depth budget. Flops with `clock = None` are **excluded from crossing detection** rather than emitting false positives.

## Crossings

A `Crossing` is the canonical unit of "one signal moves between two domains":

```python
@dataclass(frozen=True)
class Crossing:
    src_clock: str               # top-level port name, or generated clock name when pin_clocks resolved it
    dst_flop: Flop
    dst_clock: str
    min_hops: int                # comb cells on shortest path
    width: int                   # distinct dst-D bits reachable from src
    src_flop: Flop | None = None # set for register-to-register
    src_port: str | None = None  # set for typed-port-to-register
```

Key design choices:
1. **One record per (src, dst) pair**, not per bit — multi-bit buses collapse to a single `Crossing` with `width = N`
2. **`min_hops`** is the shortest path in combinational cells — `0` = direct wire, `>= 1` = comb logic before sync
3. **Source is either flop or port** — exactly one of `src_flop`/`src_port` is set. Port endpoints come from `set_input_delay -clock` in SDC

## Clock Topology

`ClockSpec` is the parsed, CDC-relevant view of an SDC file:

```python
@dataclass
class ClockSpec:
    clocks: dict[str, Clock]                       # by name
    async_groups: list[list[set[str]]]             # set_clock_groups -asynchronous
    exclusive_groups: list[list[set[str]]]         # -logically_exclusive / -physically_exclusive
    false_path_pairs: set[frozenset[str]]          # set_false_path -from -to clock pairs
    port_clock: dict[str, str]                     # port→clock mapping
    pin_clocks: dict[str, str]                     # internal-pin → generated clock name
    partial_warnings: list[str]                    # parser diagnostics
```

`pin_clocks` is populated when `create_generated_clock`'s target is `[get_pins <hier_pin>]` rather than a top-level port. The clock-trace pass keys on this map to stop walking at the pin where a forwarded clock originates, so each block in an internally-wired clock-forwarding chain gets a distinct clock identity that still resolves back to its master via `resolve()`. See [[clock-domain-tracing]] for the trace-side mechanics.

`Clock` carries `master: str | None` and `is_generated: bool` for collapsing generated clocks to their root master.

Key predicates:
- **`are_async(a, b)`** — true if unresolved names are in different async groups, OR resolved roots differ AND are in different async groups or false_path_pairs
- **`is_unreachable_crossing(a, b)`** — true if resolved roots are in different exclusive groups (clocks never coexist)
- **`resolve(name)`** — transitively walks the master chain; cycle-guarded

## Violations

```python
@dataclass(frozen=True)
class Violation:
    rule_id: str                   # "CDC-001" .. "CDC-008"
    severity: str                  # "error" | "warning" | "info"
    message: str                   # human-readable
    crossing: Crossing | None      # None for non-data rules (CDC-007, CDC-008)
    cell_name: str | None          # for source locations via cell.attributes["src"]
    instance_path: tuple[str, ...] # hierarchy path the cell lives in; () = top
```

`instance_path` is resolved from `cell_name` at the
`cli._analyze_and_report` boundary by `reporter._instance_path` —
the rule pack itself is frontend-agnostic and does not import the
reporter. `()` is the safe default and is what `Violation`
constructed directly (e.g. in tests) gets when the field is
omitted. Reporters consume the field for per-instance grouping
(text `[top]` / `u_a / u_b` headers, JSON `by_instance`, SARIF
`logicalLocations`).

## AnalysisResult

The immutable struct at the boundary between analyzer and presentation. Contains: `module`, `domains`, `crossings` (all), `async_crossings` (post-SDC filter), `spec`, `violations` (kept), `suppressed`. No formatter does additional analysis.

## Related Pages

- [[cdc-analysis-pipeline]] — how these types flow through the pipeline
- [[elaboration-frontends]] — the frontend layer that produces `Module` instances satisfying this contract
- [[clock-domain-tracing]] — `FlopDomain` and `trace_clock_root`
- [[crossing-detection]] — how `Crossing` records are created
- [[sdc-parsing]] — how `ClockSpec` is populated
- [[cdc-rule-pack]] — how `Violation` records are produced
- [[waivers-and-reporting]] — how `AnalysisResult` is consumed
