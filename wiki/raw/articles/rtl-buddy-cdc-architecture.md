---
source_url: docs/architecture.md
ingested: 2026-05-11
sha256: 07ac9423f55be20477749f1c5055877b23d7f5bdfe2925f8f38a57ced0e08d87
---

# rtl-buddy-cdc Architecture

A reference description of how `rtl-buddy-cdc` analyzes a flattened
Yosys netlist for clock-domain-crossing issues. This document covers
the data flow end-to-end, the schemas at each boundary, the algorithms
used at each pass, and the rationale behind the splits. It is the spec
to read before changing core behavior.

For an end-user introduction (CLI flags, output formats, waiver file
syntax) see [`README.md`](../README.md). This document assumes you've
read that.

## 1. Goals and non-goals

**Goals.** Catch the eight classic CDC bug shapes (CDC-001 through
CDC-008) in flattened RTL with reasonable false-positive and false-
negative rates, surface findings in formats that drop into existing
review and CI workflows (text / JSON / SARIF), stay small enough to
fork and extend in a sitting, and keep the elaboration frontend
swappable so the rule pack doesn't depend on a specific toolchain.

**Explicit non-goals.**

- Not a synthesis tool. The rule pack never invokes a synthesizer on
  its primary path — it consumes an in-memory `Module` produced by a
  frontend (see [§3.1](#31-frontend-layer)). The `analyze` command
  takes a pre-elaborated Yosys JSON; the `lint` wrapper drives a
  frontend (Yosys or slang) for source-to-analysis convenience.
- Not a timing tool. SDC is parsed for **clock topology and async
  partitioning only**; numeric delays and slack are ignored.
- Not a Tcl interpreter. The SDC parser is a deliberate `shlex`-based
  pattern matcher, not a real Tcl host (see [§6](#6-sdc-parsing)).
- Not (yet) a fully hierarchical analyzer. The netlist is still
  analysed as one flattened top, but since the #253 CDC-scaling work it
  may carry **blackbox boundary** siblings: single-clock subtrees are
  summarised to their port boundary and composed in, rather than walked
  flop-by-flop (§4.8–§4.9). This is a deliberately conservative,
  partial form — it abstracts only provably-single-clock subtrees and
  errs toward over-reporting. A full per-module hierarchical pass that
  recognises synchronisers *inside* an abstracted subtree (the
  `synchronised` refinement, §4.9) is future work, not yet present.

## 2. Pipeline at a glance

```
        SV sources + --top                     pre-elaborated
              │                                  netlist.json
              ▼                                       │
   ┌─────────────────────┐                            │
   │ frontend.elaborate  │  (Frontend.{yosys,slang})  │
   │  └─ yosys: shell    │                            │
   │     out + write_json│                            │
   │  └─ slang: pyslang  │                            │
   └────────────┬────────┘                            │
                ▼                                     ▼
        ┌──────────────────────────────────┐
        │ netlist.load        (1)          │  schema → Module / Cell / Port / Netname
        │  (yosys-frontend path only;      │
        │   slang frontend builds Module   │
        │   in-process)                    │
        └──────────────────────────────────┘
                        ▼
        ┌──────────────────────────────────┐
        │ flops.find_flops    (2)          │  recognise FF cell zoo
        └──────────────────────────────────┘
                        ▼
        ┌──────────────────────────────────┐
        │ domain.assign_domains   (3)      │  trace each CLK net → root port
        │  └─ trace_clock_root             │
        └──────────────────────────────────┘
                        ▼
        ┌──────────────────────────────────┐
        │ domain.find_crossings   (4)      │  BFS Q→reader→…→D, group per (src,dst)
        └──────────────────────────────────┘
                        ▼
   ─── design.sdc ──▶ ┌──────────────────────────────────┐
                      │ sdc.parse_file       (5)         │  ClockSpec
                      └──────────────────────────────────┘
                        ▼
        ┌──────────────────────────────────┐
        │ cli._filter_async   (6)          │  drop pairs not declared async
        └──────────────────────────────────┘
                        ▼
        ┌──────────────────────────────────┐
        │ rules.run_all       (7)          │  apply CDC-001..-008
        └──────────────────────────────────┘
                        ▼
   ─── cdc.waivers ──▶ ┌──────────────────────────────────┐
                       │ waivers.apply       (8)          │  partition into kept / suppressed
                       └──────────────────────────────────┘
                        ▼
        ┌──────────────────────────────────┐
        │ reporter.render_*   (9)          │  text / JSON / SARIF
        └──────────────────────────────────┘
                        ▼
                  exit 0 if no kept violations
                  exit 1 otherwise
```

The orchestration lives in `cli._analyze_and_report`. Steps 1–4 are
**deterministic and SDC-independent**; they always run and populate
the structural summary. Steps 5–9 only fire if an SDC was supplied —
without one the tool prints the structural summary and exits 0.

## 3. Module map

| Module | Responsibility | Key types |
|---|---|---|
| `frontend.py` | Frontend factory: pick a frontend by name, dispatch to its `elaborate` | `Frontend`, `elaborate` |
| `frontends/yosys.py` | Yosys frontend: shell out to `yosys` then `netlist.load` the JSON | `elaborate`, `YosysError` |
| `frontends/slang.py` | slang frontend: elaborate SystemVerilog via pyslang directly | `elaborate`, `SlangFrontendUnavailable`, `SlangElaborationError` |
| `netlist.py` | Parse Yosys `write_json` output into typed structs | `Module`, `Cell`, `Port`, `Netname`, `BoundarySummary`, `PortBoundary` |
| `flops.py` | Recognise the 11 Yosys FF cell variants and extract CLK / D / Q | `Flop`, `FF_CELL_TYPES` |
| `domain.py` | Trace clock roots, find flop→flop crossings | `FlopDomain`, `Crossing`, `trace_clock_root`, `find_crossings` |
| `abstract.py` | Detect + summarise single-clock subtrees to a port boundary (#256) | `is_single_clock_subtree`, `summarise_subtree` |
| `hierarchy.py` | Compositional boundary walk: summarise each shared module once (#257) | `compose_boundaries`, `CompositionStats` |
| `sdc.py` | Parse the CDC-relevant SDC subset | `ClockSpec`, `Clock` |
| `rules.py` | The CDC-001..-008 rule pack | `Violation`, `RULES`, `run_all` |
| `waivers.py` | Per-violation suppression with regex | `Waiver`, `SuppressedViolation` |
| `reporter.py` | Format an `AnalysisResult` as text / JSON / SARIF | `AnalysisResult`, `render_text`, `render_json`, `render_sarif` |
| `cli.py` | Typer entry points; orchestrates the pipeline | `analyze`, `lint`, `version` |

`__init__.py` exposes a thin `main()` shim that calls `cli.app` (used
by the console-script entry point). The other public API is
`frontend.Frontend` + `frontend.elaborate(sources, top, frontend=…)`,
which `cli.lint` consumes when the user supplies SV sources.

### 3.1 Frontend layer

`netlist.Module` is the contract every rule walks. How a module gets
built is pluggable:

- **Yosys frontend** (`frontends/yosys.py`) — runs `yosys -p
  'read_verilog ...; hierarchy -top X; proc; flatten; opt_clean;
  write_json /tmp/out.json'`, then loads the JSON via `netlist.load`.
  This is the historical primary path and remains the default for
  `lint --frontend yosys` / `analyze --netlist file.json`.
- **slang frontend** (`frontends/slang.py`) — elaborates SV sources
  via the [pyslang](https://pypi.org/project/pyslang/) binding to
  [slang](https://github.com/MikePopoloski/slang) and builds a
  `Module` directly from the elaborated `Compilation` — no
  synthesis subprocess, no `flatten` step. Opt-in via the `[slang]`
  install extra (`pip install 'rtl-buddy-cdc[slang]'`); the default
  install stays `typer`-only. When pyslang is missing the frontend
  raises `SlangFrontendUnavailable` with an install hint; fatal
  pyslang diagnostics surface through a `TextDiagnosticClient`
  (file:line:col + caret summaries — the usual compiler-error UX).
- **`Frontend.auto`** — at runtime probes
  `importlib.util.find_spec("pyslang")` via `frontend.resolve_auto`
  and dispatches to `slang` when pyslang is importable, else `yosys`.
  The default frontend stays `yosys` — `auto` is opt-in via the CLI
  (`lint --frontend auto`). The auto branch resolves at the CLI
  surface so the preamble shows the *resolved* frontend
  (`frontend: yosys (auto)`) and downstream log-scraping tools never
  see a third value in the `frontend:` line.

The factory in `frontend.py` is the only orchestration:

```python
def elaborate(sources, top, frontend=Frontend.yosys, **kw) -> Module: ...
```

Frontends produce a `Module` shape that satisfies the rule pack's
contract:

- Yosys-style cell types (`$dff`, `$adff`, `$and`, `$xor`, `$mux`, …)
- pin names `CLK` / `D` / `Q` / `ARST` on flops, `A` / `B` / `Y` on
  comb cells, etc.
- integer bit IDs for nets, with the four constant chars `"0"` /
  `"1"` / `"x"` / `"z"` reserved.
- SV `(* attr *)` declarations propagated onto the corresponding
  `Netname`.

Rules don't know which frontend produced a `Module` — the shape is
the only coupling. New frontends only need to produce that shape;
no other module changes.

The slang frontend's elaborator is the `_ModuleBuilder` inside
`frontends/slang.py`. It walks the elaborated top instance in three
passes (variables → ports → cells / continuous assigns / child
instances), lowers procedural / continuous / expression shapes to
Yosys-shape cells, and recurses into child `InstanceSymbol`s with
hierarchical name prefixes (`u_b0.q`) that match Yosys-flatten
output. Port connections alias the child's internal-port variables
to the parent's connection expression bits so net identity is
preserved across the hierarchy boundary; aliasing rewrites
propagate globally across `_var_bits` / `_ports` / `_netnames` so
chains like parent `a_q` ← child `q` collapse to a single net.
Expression shapes lower to the Yosys cell zoo (binary →
`$and`/`$or`/`$xor`/…; unary → `$not`/`$logic_not`/`$neg`/
`$reduce_*`; conditional → `$mux`; element- and range-select →
bit subsets; concat and replication → pure LSB-first bit-tuple
aliasing, no cell emitted to match Yosys post-`opt_clean`).
Every emitted cell carries an `attributes["src"]` string formatted
as Yosys' `"file:line.col-line.col"` convention, so the JSON /
SARIF reporters surface clickable source locations without a
frontend-specific branch.
Reaches parity with the Yosys frontend on every SDC-equipped
fixture in the regression suite; the per-fixture matrix lives in
the `frontends/slang.py` module docstring.

## 4. Data model

The analyzer is structured as a sequence of pure functions producing
immutable dataclasses. Frozen dataclasses are used everywhere except
where mutability is intrinsic to the algorithm (e.g. `ClockSpec`'s
parser-built collections).

### 4.1 Netlist layer

A `Module` is a flat namespace of `ports`, `cells`, and `netnames`,
each indexed by name. Connections are sequences of `Bit`, where a
`Bit` is either an `int` (the Yosys net ID) or one of the constant
strings `"0"`, `"1"`, `"x"`, `"z"`. The integer-vs-string distinction
is load-bearing — every walker ignores constant bits because they
can't propagate driver identity.

`Netname` is the wire/reg name as it appeared in the source, with any
SV `(* attr = "value" *)` annotations the user attached. Yosys
preserves attributes on the netname rather than the cell that drives
the bits, so the rule pack maps from a tagged netname's bits back to
the upstream flop (see `user_sync_flop_names` and
`user_gray_flop_names` in `rules.py`).

### 4.2 Flop recognition

`flops.FF_CELL_TYPES` enumerates the FF cell zoo Yosys may emit:
`$dff`, `$dffe`, `$adff`, `$adffe`, `$aldff`, `$aldffe`, `$sdff`,
`$sdffe`, `$sdffce`, `$dffsr`, `$dffsre`. Each variant has different
reset/enable plumbing, but **CLK / D / Q are universal** — those are
the only pins the analyzer needs for domain assignment and fanout
walks. The reset pin is exposed on `Flop` for future reset-CDC rules
but unused in CDC-007 today (which works off ARST connections at the
cell level).

### 4.3 Domain assignment

`FlopDomain` ties a `Flop` to a `clock: str | None` — the top-level
input port name that ultimately drives its CLK pin, or `None` if no
root could be resolved within the trace depth budget. The analyzer
treats `None` as "domain unknown" and excludes those flops from
crossing detection rather than emitting a false positive.

### 4.4 Crossings

A `Crossing` is the canonical unit of "here is one place a signal
moves between two domains":

```python
@dataclass(frozen=True)
class Crossing:
    src_clock: str               # top-level port name (or clock name for typed ports)
    dst_flop: Flop
    dst_clock: str
    min_hops: int                # # comb cells on shortest path
    width: int                   # # distinct dst-D bits reachable from src
    src_flop: Flop | None = None # set for register-to-register
    src_port: str | None = None  # set for typed-port-to-register
    src_boundary: tuple[str, str] | None = None  # (instance, port): abstracted-subtree output
    dst_boundary: tuple[str, str] | None = None  # (instance, port): data entering a boundary input
```

Three design choices matter here:

1. **One record per (src, dst) pair**, not per bit. A multi-bit bus
   crossing collapses to a single `Crossing` with `width = N`. This
   is how rules like CDC-004 ("multi-bit bus crossing") get a width
   directly without re-walking.
2. **`min_hops` is the shortest path length in *combinational cells*
   crossed.** `min_hops == 0` means a direct flop-to-flop (or
   port-to-flop) wire — the classic sync first stage. `min_hops >= 1`
   is what CDC-003 ("combinational logic before sync") fires on for
   flop sources.
3. **Source endpoint is a flop, a port, or an abstracted boundary.**
   Exactly one of `src_flop`, `src_port`, and `src_boundary` is set.
   Port endpoints are emitted by `find_crossings(module,
   port_clock=...)` when the SDC has typed the port via
   `set_input_delay -clock <c>`. `src_boundary = (instance, port)`
   endpoints come from an auto-abstracted single-clock subtree
   (§4.8 / §7.4): the summarised subtree's output port is re-seeded
   as a virtual source at the subtree's domain, standing in for the
   flattened internal flops that no longer exist in the netlist. The
   convenience property `src_name` returns the flop name, `"port
   <p>"`, or `"boundary <inst>.<port>"` for messages and waiver
   matching. `dst_boundary = (instance, port)` is the symmetric field
   (P3 / #257) for data *entering* a boundary input in a domain foreign
   to the boundary's own clock: `find_crossings` seeds a synthetic
   virtual *sink* flop standing in for the boundary input pin (§7.4), so
   the crossing the flattened subtree would have reported at its first
   internal flop is preserved under abstraction. The single `Crossing`
   type carries both `src_boundary` and `dst_boundary` as additive
   optional fields so the public JSON contract (`summary.crossings`) is
   never forked.

### 4.5 Clock topology

`ClockSpec` is the parsed, CDC-relevant view of an SDC file:

```python
@dataclass
class ClockSpec:
    clocks: dict[str, Clock]                       # by name
    async_groups: list[list[set[str]]]             # set_clock_groups -asynchronous
    exclusive_groups: list[list[set[str]]]         # -logically_exclusive / -physically_exclusive
    false_path_pairs: set[frozenset[str]]          # set_false_path -from -to clock pairs
    port_clock: dict[str, str]                     # set_input_delay / set_output_delay → port→clock
    pin_clocks: dict[str, str]                     # internal-pin target → generated clock name
    partial_warnings: list[str]                    # diagnostics surfaced once at end-of-parse
```

`pin_clocks` is populated when a `create_generated_clock` target is a
`[get_pins <hier_pin>]` expression rather than a top-level port. It is
consumed by `trace_clock_root` (§5) to stop the clock walk at the pin
where a forwarded clock takes over, so each block in a source-sync
chain wired through internal nets gets a distinct clock identity that
still resolves back to its master via `resolve()`.

`Clock` carries `master: str | None` and `is_generated: bool` so the
analyzer can collapse generated clocks (dividers, PLL outputs) back
to their root master.

Three consumer-facing predicates:

- `are_async(a, b)` — two clocks are async if (1) the SDC explicitly
  places their **unresolved names** in different groups of the same
  `set_clock_groups -asynchronous` statement (the explicit override
  case for generated clocks), OR (2) their resolved roots differ
  AND `false_path_pairs` lists them OR async groups separate the
  resolved roots. Step 1 is what lets a project mark `clk_div2` as
  async to `clk` despite sharing a master.
- `is_unreachable_crossing(a, b)` — true iff resolved roots are in
  different `exclusive_groups` entries. Logically/physically-
  exclusive clocks never coexist at runtime, so a flop→flop
  "crossing" between them is a static-analysis artifact, not a real
  path. `_filter_async` consults this **before** `are_async` and
  drops unreachable crossings entirely.
- `resolve(name)` — collapses a generated clock to its root master
  by walking the `master` chain transitively. Cycle-guarded.

Clocks not mentioned in any group remain conservatively synchronous
to everything (zero false positives for crossings the user has not
declared async). See [§6](#6-sdc-parsing) for the parser scope.

### 4.6 Violations

```python
@dataclass(frozen=True)
class Violation:
    rule_id: str                   # "CDC-001" .. "CDC-008"
    severity: str                  # "error" | "warning" | "info"
    message: str                   # human-readable
    crossing: Crossing | None      # the offending crossing (most rules)
    cell_name: str | None          # most-responsible cell (for src locations)
    instance_path: tuple[str, ...] # hierarchy path the cell lives in; () = top
```

`crossing` is `None` for rules that operate on a non-data shape
(CDC-007 reset crossings, CDC-008 clock-as-data). `cell_name` lets
structured reporters surface a `file:line:col` source location by
looking at `cell.attributes["src"]`.

`instance_path` is the dot-stripped, tuple-form hierarchy the
offending cell lives in. Resolved from `cell_name` at the
`cli._analyze_and_report` boundary (the rule pack stays
frontend-agnostic — no `reporter` import in `rules.py`); see
[`wiki/raw/articles/hierarchical-reporting.md`](hierarchical-reporting.md)
for the resolver contract. `()` is the safe default (top instance)
and is also what rules constructing a `Violation` directly get
without specifying — the boundary post-pass overrides it for every
finding that actually flows through `_analyze_and_report`.

### 4.7 The reporter contract

`AnalysisResult` is the immutable struct that flows into every
formatter. It contains everything any output mode might need:
`module`, `domains`, `crossings` (all of them), `async_crossings`
(after SDC filter), `spec`, `violations` (kept), `suppressed`. No
formatter does additional analysis — this struct is the boundary
between "analyzer" and "presentation".

#### Under-resolution visibility (issue #263)

A flop whose clock root the tracer cannot resolve carries
`FlopDomain.clock is None` and is **excluded from crossing detection** —
a crossing into or out of it cannot be classified. On large netlists a
non-trivial fraction of flops can land here, silently shrinking
coverage. The reporter surfaces this as a report-only diagnostic
(it never changes a classification):

- JSON `summary.domain_unknown` (int) — count of `None`-clock flops.
  This key is pinned in `JSON_CONTRACT` so downstream `rtl_buddy` can
  treat a non-zero value as coverage degradation rather than a clean run.
- JSON `domain_unknown_flops` (list) — a bounded sample (first
  `_DOMAIN_UNKNOWN_SAMPLE_CAP`, currently 20) of the unresolved flops'
  cell names, in `domains` order, to point at the under-resolved
  subtrees. The full count is always `summary.domain_unknown`.
- Text report — when the count is non-zero, a prominent `⚠ N of M flops
  have unresolved clock domain — excluded from CDC analysis` line, so an
  under-resolved run no longer reads as a complete one.

### 4.8 Blackbox boundaries and the compositional data model

The CDC-scaling work (epic #253) makes a large subtree analysable at
its **port boundary** instead of flattening it flop-by-flop. The data
model carries this without a sibling type — two optional, default-empty
fields extend the existing frozen `Module`, so every existing `Module`
consumer (`find_flops` / `assign_domains` / `find_crossings` / the rule
pack) keeps working untouched:

```python
@dataclass(frozen=True)
class Module:
    name: str
    ports: dict[str, Port]
    cells: dict[str, Cell]
    netnames: dict[str, Netname]
    is_blackbox: bool = False            # P1 #255: from attributes.blackbox
    boundary: BoundarySummary | None = None  # attached by the P2 summariser
```

A blackboxed subtree arrives as an ordinary Yosys-JSON module carrying
a truthy `attributes.blackbox` bit-string and **zero cells** — it
survives `flatten` with its real (non-`$`) name (verified by the P1
prep probe), and the parent keeps each instance as an ordinary `Cell`
whose `type` is the blackbox module name. So there is no new cell type
and no parent rewrite. `netlist.load` sets `is_blackbox` from the
attribute; `boundary` is attached later by the summariser, never by
`load`.

The boundary is described by its output/inout ports (virtual sources)
**and** its input/inout ports (virtual sinks), plus the subtree's own
resolved clock domain:

```python
@dataclass(frozen=True)
class PortBoundary:
    port: str             # port name (matches Module.ports key)
    src_clock: str | None # output: domain driving the port; input: subtree capture domain
    synchronised: bool    # True iff every register→port path passes a synchroniser
    width: int            # number of bits on the port

@dataclass(frozen=True)
class BoundarySummary:
    module: str                          # the summarised module's real (non-$) name
    ports: dict[str, PortBoundary]       # output/inout ports (virtual sources)
    clock: str | None = None             # P3 #257: the subtree's own resolved clock
    input_ports: dict[str, PortBoundary] = {}  # P3 #257: input/inout ports (virtual sinks)
```

For an **output** port, `synchronised=True` means a downstream sink in
`src_clock` is *not* a crossing (the subtree retimes internally);
`synchronised=False` means a sink in a different domain *is* a crossing
the parent must check; `src_clock=None` is the conservative unconstrained
source that crosses to any known-domain sink (mirroring the
`<unconstrained>` sentinel).

For an **input** port (P3 / #257), `src_clock` names the boundary's own
clock domain (`BoundarySummary.clock`) — what the subtree's first
internal flop captures the input on. Clock pins are excluded (they are
distribution, not data, and must never become a virtual sink — that
would re-introduce the CDC-008 clock-as-data shape). `find_crossings`
seeds a synthetic virtual-sink flop at each input port's connected bits,
so foreign-domain data entering the boundary is reported as a
`dst_boundary` crossing rather than vanishing under abstraction. This is
the mirror of the output-port virtual-source seeding and is what makes
abstracting a single-clock subtree with a foreign-domain input
*result-preserving* — the P2-era refusal to abstract such subtrees is
retired.

**Relaxed single-module invariant.** `netlist.load_with_blackboxes`
relaxes the historical "exactly one non-`$` module after flatten" rule
to "exactly one non-`$` **non-blackbox** module (the top) + zero-or-more
blackbox sibling modules". It returns `(top, blackboxes)` where
`blackboxes` is a `dict[str, Module]` keyed by module name so the top's
boundary cells resolve their summary. Two non-`$` non-blackbox modules
is still ambiguous and raises. `netlist.load` stays as the
back-compat single-return entry point (drops the siblings).

See §4.9 for what this collapse preserves versus discards, and for the
status and roadmap of the (currently inert) `synchronised` field.

### 4.9 What abstraction preserves, what it collapses, and the `synchronised` hook

**The boundary is a star-collapse onto one clock domain, not an
input→output graph.** A summarised subtree is reduced to a single
structural claim — *everything inside is clock domain `D`*
(`BoundarySummary.clock`). Every output/inout port becomes a virtual
**source** in `D`; every data input/inout port becomes a virtual
**sink** captured in `D`. The subtree's *internal* connectivity — which
input fans out to which output, and whether a path is registered or
purely combinational — is **discarded by design**. There is no per-port
input→output relation in the summary.

This collapse **severs** internal connectivity; it does not
*fully-connect* it. A foreign-domain input is reported as captured
*into* `D` and goes no further — its virtual sink is a terminal flop
with an empty `Q` (`domain.py` stamps `q=()`), so nothing propagates out
of it. Each output is launched *from* `D` as an independent seed that
walks only the parent's real fanout. The two never chain through a
shared `D` hub. So the model never manufactures a path between an input
and an *unrelated* output: given real paths `A→X` and `B→Y` but no `A→Y`
/ `B→X`, **no spurious `A→Y` / `B→X` crossing appears** — output `X`
still feeds only `X`'s real parent sinks, output `Y` only `Y`'s. What
the collapse loses is the *attribution* tying a specific input's domain
to a specific output (everything is mediated by `D`), not the separation
between unrelated ports. The over-reporting is therefore **per-port
(linear)** — a foreign input `X_d→D` here, an output `D→sink` there —
never an input×output **mesh (quadratic)**.

Only the *subtree-internal* graph collapses. The **parent-side** fanin
and fanout of the boundary ports are untouched: they are real nets in
the flattened top, and `find_crossings` seeds the virtual source/sink
onto those nets and walks the parent exactly as it would for any flop or
port. A crossing from a boundary output into downstream parent logic, or
from upstream parent logic into a boundary input, is found precisely.

Precision characterisation:

- **Exact (result-preserving)** for genuine single-clock *sequential*
  logic — inputs registered before use, outputs driven by registers. The
  flattened flops would show inputs captured in `D` and outputs launched
  from `D`, which is exactly what the summary asserts. This is the target
  case and the reason the collapse is safe.
- **Over-approximating but never under-reporting** for *combinational
  feed-through*. A foreign-domain input (`X`) that passes combinationally
  to an output and on to a sink in domain `E` is one real path `X→E`; the
  model splits it into two fictitious hops `X→D` (at the input sink) and
  `D→E` (at the output source). This can raise a **false positive** —
  e.g. a single-bit input that returns to a sink in its *own* domain is
  flagged at both hops though the real circuit has no crossing — but it
  can never *drop* a real crossing. For a CDC checker that is the correct
  direction to err. (This is the documented conservative edge from the
  #253 epic PR.)
- **Never silently collapses a real internal crossing.**
  `is_single_clock_subtree` refuses to abstract any subtree it cannot
  prove sits in one async-safe domain (multiple async clocks, an
  unresolved/forwarded clock, or differing roots not declared
  synchronous); such a subtree is walked flat. Abstraction applies only
  where there is provably no internal crossing to lose.

**Three soundness guards close the silent false-negatives the #259 audit
found.** All three err toward *declining* abstraction — the conservative
direction for a CDC checker.

- **Traced multi-clock decline (`abstract._instance_clocks`).** The
  subtree's clock SET is determined from *all* of the blackbox module's
  input ports, not a clock-pin **name** allow-list. A port is a clock pin
  when its name is in `_CLOCK_PIN_NAMES`, **or** it is conventionally
  clock-named (`wr_clk` / `rd_clk` / `clk_a` / `core_clock` — the
  `_CLOCK_NAME_HINTS` substrings) *and* its parent-side driver traces to a
  declared clock through **clock-network cells only** (buffers / gates /
  muxes / ports — `trace_clock_root(..., allow_divider=False)`, so a data
  net launched by a flop `Q` is **not** misread as a clock). The full set
  of distinct clock roots flows to `is_single_clock_subtree`, so a
  dual-clock IP whose clock pins fall outside the name allow-list (an
  async FIFO's `wr_clk` / `rd_clk`) presents ≥2 roots and is **declined**
  — its internal clkA→clkB crossing is no longer silently abstracted away
  as if the block were single-clock / combinational. The single capture
  `clock` is the sole root (or `None` for a genuinely combinational
  boundary), and the traced clock-pin set — not just the name allow-list —
  is what is excluded from input-sink seeding. `compose_boundaries` keys
  its summary cache on the **frozenset of clock roots**, so a dual-clock
  instance keys distinctly while identical instances still hit the cache.

- **Declined-opaque `CDC-BBX` error (FIX 2).** A declined instance is
  absent from the boundary map; its zero-cell internals are unanalysed —
  a coverage gap. Each declined instance (from
  `CompositionStats.declined_modules`, plus the reconvergence-declined set
  below) is emitted as a per-instance **`error`-severity `Violation`** with
  `rule_id = "CDC-BBX"` and `cell_name = <instance>`, folded into the
  `violations` list before waiver/baseline/exit-code processing. So it is
  rendered in every format, **fails the run by default** (exit 1), and is
  **waivable** — intentional opacity (a separately signed-off IP) is
  acknowledged with `waive CDC-BBX <instance-regex>`, moving it to the
  suppressed tally. The silent drop becomes a fail-by-default, explicitly
  acknowledged one. (`CDC-BBX` is emitted by the CLI orchestration, not the
  rule pack — it is an analysis-coverage finding, not a structural rule.)

- **Reconvergence-unsafe skip (`hierarchy.reconvergence_unsafe_instances`,
  FIX 3).** A single-clock block that *is* abstracted but has ≥2 distinct
  foreign-domain crossings entering **distinct** input ports can hide an
  internal reconvergence (CDC-005) the flat design would flag — the
  star-collapse severs the subtree-internal graph, so that reconvergence
  cannot be checked at the boundary. After `find_crossings`, boundary-sink
  crossings (`dst_boundary`) are grouped by instance; an instance with ≥2
  distinct incoming ports is reconvergence-unsafe. Such instances are
  removed from the boundary map and `find_crossings` is **re-run** so the
  block becomes opaque (no boundary crossings emitted for it), and the
  instance is emitted as the same per-instance `CDC-BBX` error (`… has
  crossings into N input ports; reconvergence among them cannot be checked
  at the boundary — …`), waivable like any other. A
  single multi-bit bus on **one** port counts as one port (safe — the
  multi-bit rules cover it).

CDC-008's blackbox exemption is correspondingly per-CLOCK-PIN (FIX 4),
not whole-instance: only the instance's traced clock pins (or the
`_CLOCK_PIN_NAMES` fallback) are exempt from clock-as-data, so a clock
wired into a genuine **data** input of a blackbox still fires CDC-008.

**The `synchronised` field is the seam to a more precise model — and is
presently inert.** `PortBoundary.synchronised` is hard-wired `False` by
the summariser (`abstract.py`) and is not yet consumed (the
`find_crossings` boundary loop only reserves space for it: *"synchronised
ports never reach here — the summariser drops them before they become a
boundary"*). So today the model assumes *no* boundary path is
synchronised — the direct source of the conservative over-reporting
above. Wiring it live means:

- **Effect of `True`.** The summariser omits that port from `ports` /
  `input_ports`, so it never becomes a virtual source/sink and raises no
  crossing (a softer variant emits a benign *recognised-synchroniser*
  finding instead of dropping it). Two directions: an **output** with a
  built-in synchroniser (clean data leaves the IP), or an **input** whose
  first internal stage is a proper two-FF synchroniser (the boundary
  crossing is the legitimate, handled one — this is what would retire the
  single-bit CDC-001 over-report).
- **Detection is the hard part.** Proving a port synchronised means
  running the existing synchroniser recognition (`_sync_chain_depth`,
  `user_sync_flop_names`) over the subtree's register→port paths — but
  the blackbox has **zero cells** by construction, so its internals are
  not in the flat netlist. The bit must come from either **(a)** a real
  per-module analysis pass — analyse each subtree *module* once in
  isolation, detect its boundary synchronisers, and record `synchronised`
  per port in the cached `BoundarySummary` (the compositional vision of
  `compose_boundaries`, which already caches by `(module-type,
  clock-context)`); or **(b)** a user annotation reusing the
  `USER_SYNC_ATTRS` mechanism (or a `cdc.yaml` boundary declaration),
  asserting "module *M*'s output *O* is synchronised" — the boundary
  analogue of `user_sync_flop_names`.
- **Soundness asymmetry.** Every other approximation in this model errs
  toward over-reporting. `synchronised=True` is the *only* lever that can
  make the tool **under**-report (trust a synchroniser that is not
  there), so it may be set only when *proven* (route a) or *explicitly
  promised* (route b), and it defaults `False`. The present pessimism is
  the deliberate price of that default until one of the two detection
  sources exists.

## 5. Clock-domain tracing

`domain.trace_clock_root` is the heart of CDC-008 and the foundation
for everything else: every flop's domain identity comes from chasing
its CLK net upstream until a top-level input port is hit. The walker
has to handle the common clock-network shapes without false-tagging
data signals as clocks.

Cell-type categories the walker recognizes:

| Category | Yosys cell types | Behavior |
|---|---|---|
| Buffer / inverter | `$not`, `$logic_not`, `$buf`, `$pos`, `$reduce_bool`, `$_BUF_`, `$_NOT_` | Transparent: trace the single `A` input |
| Two-input gate (ICG) | `$and`, `$or`, `$logic_and`, `$logic_or`, `$xor`, `$xnor` | Trace both inputs; the side that resolves to a clock is the root, the other is the gate's enable |
| Mux | `$mux`, `$pmux` | Trace each candidate input; first one to resolve wins (the analyzer can't statically know which side `S` selects) |
| FF (clock divider) | any of `FF_CELL_TYPES` reaching from a `Q` output | Recurse on the source flop's CLK pin; the divided clock inherits its domain identity from the upstream root |
| Transparent latch (clock-path ICG) | `$dlatch`, any `$_DLATCH_*` reaching from a `Q` output | Explore the latch's data pin (`D`) and enable pin (`EN` coarse / `E` gate-level), first one to resolve wins (`D` before `EN`). A clock routed through a latch-based ICG can enter on either pin depending on the coding style. **Clock-resolution only** — latch transparency here never reaches data-path crossing detection, which keeps treating a latch as an opaque endpoint (CDC-017). See issue #263 (P2) |

The walker maintains a per-call `seen` set keyed on bit ID and a
`max_depth` counter (default 16), so cycles in the clock network
terminate cleanly. The depth budget is intentionally low — clock
networks rarely exceed a handful of hops, and a deep walk is more
likely to be following data than clock.

**First-resolves-wins on multi-input clock cells (audit note).** The
two-input gate clause and the clock-path latch clause both explore
several inputs and return the *first* that resolves to a clock root
(gate: `A` before `B`; latch: `D` before `EN`/`E`). For a legitimate
single-clock ICG this is exact — only one input is a clock, the other
is a data enable that does not trace to a clock root. For a
*pathological* cell that genuinely combines two **different** clock
roots (e.g. an `$and` with `A=clkA, B=clkB`, or a latch with
`D=clkA, EN=clkB`), the walk reports the first leg and the downstream
flop reads as that one domain. This is the established, conservative
gate-clause behaviour, deliberately matched by the latch clause for
consistency; it is sound for crossing detection (a flop labelled
clkA still crosses against every clkB source) but does not *itself*
flag the multi-clock combine — a stricter clock-combine rule is left
to future work. The first-resolves-wins choice never makes a crossing
disappear: it only assigns a previously domain-unknown flop a verified
upstream clock root.

The budget is configurable. `assign_domains(module, ...,
max_depth=N)` and `find_crossings(module, ..., max_depth=N)` forward
`N` to `trace_clock_root`; the CLI surfaces it as
`--clock-trace-depth` on both `analyze` and `lint` (default 16). A
deep clock tree — a long divider / buffer / ICG chain — can exceed
16 hops and leave its downstream flops domain-unknown (counted in
`summary.domain_unknown`, §8.x); raising the budget resolves them
without a code change. The change is monotone: a larger budget can
only resolve **more** flops, never fewer, so the default leaves every
result identical to a fixed-16 walk. The crossing walk's own
`max_hops` data-fanout budget (§7) is a separate concern and is not
affected. See issue #263.

`--clock-trace-depth` threads to **every** clock-root trace on a run,
not only the crossing walk. The boundary-abstraction decision
(`compose_boundaries` → `summarise_subtree` → `_instance_clocks`,
§4.8) and the clock-network surface (`find_clock_network_crossings`,
§8) and the rule-context per-flop domain view (`run_all` →
`_build_context`) all take the same `max_depth`. This is a
**soundness** requirement, not a convenience: if the abstraction
decision ran at a fixed 16 while the crossing walk ran at a raised
depth, a dual-clock blackbox whose second clock pin is fed through a
>16-hop clock chain would present only its shallow root, look
single-clock, and be abstracted away — silently dropping its internal
async crossing (the false-negative §4.9 forbids). Because both sides
share the budget, the abstraction can never collapse a boundary the
crossing walk would keep multi-clock.

### 5.1 Internal-pin generated clocks

`trace_clock_root` accepts an optional `bit_to_clock` short-circuit
map. When the walk lands on a bit in the map, it returns that clock
name immediately instead of continuing back to a top-level port.

The map is built by `_build_bit_to_clock(module, pin_clocks)` from
`ClockSpec.pin_clocks` (§4.5): for each SDC pin path (e.g.
`u_a/clk_out`), the helper normalises `/`→`.` to match Yosys'
flattened netname convention, looks up the netname, and harvests
every integer bit of that net. `assign_domains(module,
pin_clocks=...)` and `find_crossings(module, ..., pin_clocks=...)`
build and thread the map automatically when invoked from the CLI.

This models SoC clock-forwarding chains where each block declares its
forwarded clock with `create_generated_clock` at an internal pin.
Without it, every flop downstream of the forwarding block's clock
buffer would collapse to whichever top input port feeds the chain,
making the per-block SDC declarations inert. The returned name is a
generated-clock identity (`ck_b0`), not a port; downstream consumers
handle this transparently because `resolve()` already collapses
generated clocks to their root master, so async-pair checks behave
identically whether the clock identity came from a port or a pin.

A practical gotcha when building fixtures: Yosys' Verilog frontend
algebraically aliases trivial assign chains (`assign x = y; assign z
= ~~x;`), so a pin like `u_a/clk_out` declared on the output of a
soft buffer can end up sharing a net bit with the upstream port — at
which point the pin map collapses to a single bit and every flop in
the design traces to whichever clock won the `setdefault` race.
Forcing a real gate-level cell (`$_BUF_`, `$_NOT_` pair, or an
attribute-kept primitive) on the forwarding path keeps each forwarded
clock at a distinct bit identity. The `good_source_sync_internal`
fixture uses `$_BUF_` primitives for this reason.

CDC-008 ("clock signal used as data") asks a closely related
question — *which cells form the legitimate clock-distribution
network?* — and answers it with its own helper,
`rules._clock_network_cells`. That helper is a separate reverse-BFS
from each flop's CLK pin (different drivers-map shape, different
visit shape) but conceptually mirrors the buffer/ICG/mux/divider
categories above. Cells flagged by it are exempt from CDC-008
because legitimate ICGs, clock muxes, and dividers all read clocks
as inputs. The two walkers live in different modules deliberately —
`domain.py` is responsible for clock identity, `rules.py` for the
rule's own structural detection — and any change to the clock-cell
taxonomy needs to land in both.

## 6. SDC parsing

`sdc.py` is intentionally **not a Tcl interpreter**. SDC is Tcl
syntactically, but the CDC-relevant subset is small enough that a
hand-rolled Tcl-aware tokenizer plus a per-command arg-spec table is
the right tool: real Tcl interpreters (`tkinter.Tcl()`) execute user
code, complicate deployment, and add a non-Python dependency to the
wheel. The trade-off and the conditions under which we'd revisit it
are recorded on rtl-buddy-cdc#144.

The implementation has two layers, introduced together in the #144
rewrite (after the pointwise fixes for #140 and #142 made clear the
old shlex-based shape was generating bugs of the same class):

1. **Layer 1 — Tcl word tokenizer** (`_tokenize`). Consumes the SDC
   source and emits a list of word lists, one per logical command
   line. ``{...}`` braces and ``[...]`` brackets are single tokens
   with nesting respected, ``\<newline>`` collapses line continuation
   to a space, ``"..."`` strips quoting (backslash escapes the next
   character inside), ``#`` at a word boundary comments to
   end-of-line. The single-token form ``{ck_a}`` / ``[ck_a]`` —
   ground zero for #142 — comes through as one word, not two.

2. **Layer 2 — Per-command arg-spec table** (`ARG_SPECS`). Each
   supported command declares its flags with arity:
   `Arity.ZERO` (bare flag), `Arity.ONE` (flag plus one word), or
   `Arity.GREEDY` (flag slurps non-flag words until the next
   `-flag`). `_slice` walks the word list against the spec and
   returns a `Parsed(flags, tail)` bag the handlers consume
   directly. No per-command "walk forward until the next `-flag`"
   loop — that pattern, used in the pre-#144 parser, is what
   produced #140 (the source-swallowing-target bug).

The collection-valued operands every command takes — port lists,
clock lists, pin lists — classify as `Arity.ONE` because the
tokenizer hands them through as a single word: `[get_ports clk]` is
one word, `{ck_a ck_b}` is one word, `clk` is one word.
`Arity.GREEDY` exists for `-group`, `-from`, and `-to` so the parser
tolerates the un-collection form (`-group ck_a ck_b` without braces)
that some SDC files in the wild use.

### 6.1 What the parser handles today

```text
create_clock -name <name> -period <p> [get_ports <port> ...]
create_generated_clock -name <n> -master_clock <m> \
    -source <pin-or-port> -divide_by N [get_pins <pin>]
set_clock_groups -asynchronous          -group {…} -group {…} …
set_clock_groups -logically_exclusive   -group {…} -group {…} …
set_clock_groups -physically_exclusive  -group {…} -group {…} …
set_false_path  -from [get_clocks A] -to [get_clocks B]
set_input_delay  -clock <name> … [get_ports <port>]
set_output_delay -clock <name> … [get_ports <port>]
```

Every collection-valued operand on every supported command accepts
all three Tcl-flavoured forms: bracket (`[get_ports clk]`), brace
(`{clk}` — single or multi-token), and bare identifier (`clk`). The
test suite pins each form for each command in
`tests/test_sdc.py` (see the "issue #144: brace / bracket / bare
form coverage per command" section).

Plus: `#` comments, `\<newline>` line continuation, and a permissive
flag-skipping pattern for unrecognised options on otherwise-known
commands (so vendor-specific dialects don't choke). The unknown-flag
heuristic in `_slice`: if the next token doesn't look like another
flag, the unknown flag is assumed to take one operand and both are
skipped; otherwise only the flag itself is skipped.

Generated clocks fold back into their master via `ClockSpec.resolve`
unless an explicit `set_clock_groups -asynchronous` overrides the
relationship. When a `create_generated_clock` target is a `[get_pins
<hier_pin>]` expression rather than a top-level port, the pin path is
recorded in `ClockSpec.pin_clocks` and consumed by the clock walker
(§5.1) — that's the integration point for SoC clock-forwarding
chains. `set_false_path -from [get_clocks A] -to [get_clocks B]` is
treated as a pairwise async hint (equivalent to async groups for that
specific pair). Exclusive groups drop crossings as unreachable in
`_filter_async` before any rule sees them.

### 6.2 What it deliberately ignores

STA-only commands (`set_max_delay`, `set_min_delay`, `set_load`,
`set_drive`, `set_disable_timing`, `set_case_analysis`, …) are
silently dropped at the `logging.DEBUG` level. This is by design:
users should be able to point the tool at their existing constraint
file without curating a CDC-only subset.

The parser is also deliberately *not* a Tcl interpreter (see this
section's intro). Constructs not supported: command substitution
beyond `[get_clocks …]` / `[get_ports …]` / `[get_pins …]`, `set`
variables, `expr`, `-filter` clauses inside collection commands,
and `set_false_path -through` (path-specific, not a clock-pair
hint). If any of these become real requirements, the right move is
to switch to `tkinter.Tcl()` (the rejected alternative recorded on
#144), not to grow them onto this tokenizer.

### 6.3 Diagnostics policy

The silent-drop default is tempered by `ClockSpec.partial_warnings`:
when the parser sees a CDC-relevant command it can't fully
understand (e.g. `set_false_path -through`, `set_clock_groups`
without a kind specifier, `[get_clocks -filter …]`, `set_false_path`
with non-clock endpoints), it appends a one-line description. The
CLI surfaces these once at the end of parsing (in text mode, to
stderr) rather than logging line-by-line — keeping the noise floor
low for the common case while flagging cases where the user might
assume CDC coverage that isn't there.

Truly unrecognised commands (`set_load`, `set_drive`, …) emit only
a `logging.DEBUG` line so they're visible under `--verbose` but
don't pollute normal output.

## 7. Crossing detection

`domain.find_crossings` is the BFS that turns "every flop is in a
clock domain" into a list of `Crossing` records.

### 7.1 The walk

For each source flop with a known clock:

1. Start the BFS frontier at every bit on its `Q` pins, hop count 0.
2. For each `(bit, hops)` on the frontier:
   - If `bit` is connected to any flop's `D` pin in a *different*
     clock domain, record the crossing (or update the existing record
     for that `(src_flop, dst_flop)` pair).
   - Else, push `bit` through every consumer cell that isn't a flop,
     emitting that cell's outputs at `hops + 1`. Flops are skipped as
     transit nodes — landing on a `D` pin is the only way the BFS
     terminates productively.
3. Hop budget is `max_hops = 4` by default. Beyond that, signals are
   treated as no longer "directly connecting" the two flops; the rule
   pack can't reason about the synchronizer shape past that depth.

### 7.2 Grouping

Crossings are grouped by `(src_flop_name, dst_flop_name)` as the BFS
runs. The first time a destination-flop D-pin is hit, a new record is
created with the current bit and hop count; subsequent hits on the
same pair widen `width` and lower `min_hops`. This is what makes
multi-bit buses produce one record with `width = N` instead of N
records with `width = 1`.

### 7.3 What's intentionally excluded

- **Flop→port crossings.** When a flop drives a top-level *output*
  port directly, no record is emitted. The shape that matters there
  is "comb logic on the way out" (CDC-006-class) and the
  responsibility shifts to the consumer of the port; we leave
  output-side checking to that layer.
- **Untyped-port crossings.** Ports without `set_input_delay -clock`
  are not emitted as port-sourced `Crossing` records. The legacy
  CDC-006 path still fires on them (via `_backward_fanin` walking
  back from synchronizer first stages), so coverage is preserved —
  we just don't promote them, since without a typed clock the
  "is this async?" question has no SDC-grounded answer.
- **CLK pins as transit bits.** `_build_bit_consumers` filters out
  clock connections so the data BFS doesn't follow them.
- **Same-domain crossings.** Filtered out at record-creation time —
  if `dst_clock == src_clock`, no record is created.

### 7.4 Boundary re-seeding and the compositional walk

When the design carries auto-abstracted blackbox boundaries (§4.8),
`find_crossings(module, boundaries=...)` re-seeds the boundary crossings
the flattened subtree would have produced, on both sides:

- **Output side (virtual sources).** A third pass alongside the
  flop-source and port-source walks: for each boundary instance and each
  summarised *output* port it seeds a BFS frontier at the port's
  connected bits — exactly the port-walk shape — and emits a
  `src_boundary = (instance, port)` `Crossing` for every foreign-domain
  sink it reaches. A `synchronised=True` port never reaches this pass
  (the summariser drops it before it becomes a boundary); a
  `src_clock=None` port seeds the `<unconstrained>` sentinel so it
  crosses to any known-domain sink.
- **Input side (virtual sinks, P3 / #257).** For each summarised *input*
  port a synthetic sink flop standing in for the boundary input pin is
  folded into the domain map (`D` = the port's connected bits, clock =
  `BoundarySummary.clock`). The flop-, port-, and output-boundary walks
  then reach it with no special-casing; on landing, the emitting site
  stamps `dst_boundary = (instance, port)`. So foreign-domain data
  *entering* the boundary is reported as a crossing into it — the
  crossing the flattened subtree's first internal flop would have
  carried. The synthetic flop is never in `module.cells`, so the rule
  context built from the real netlist is untouched; its cell `type`
  (`$boundary_sink`) is deliberately not a recognised FF type, so
  CDC-008's clock-pin exemption and the dffe-EN gating check treat it as
  an ordinary opaque destination.

Both walks skip `$scopeinfo` transit cells and treat flops as sinks
(never tunnel through them), identically to the flop/port walks.
`find_crossings` resolves each boundary cell's summary **instance-first**
(falling back to a module-type key for legacy hand-built callers), so the
per-instance map `compose_boundaries` returns is consumed directly.

The **compositional** half lives in `hierarchy.compose_boundaries`
(the per-module driver, subtasks 3a/3b of #257). Given the top module,
its blackbox siblings, and the `ClockSpec`, it walks the boundary
instances and summarises each distinct `(module type, clock context)`
**once** — caching by that pair. Every later instance with the *same*
context is served from cache, so a block instantiated N times in one
domain costs one `abstract.summarise_subtree` call, not N, and the full
flattened subgraph is never materialised. The returned boundary map is
keyed **per instance** (the cell name) so `find_crossings` re-seeds each
instance's crossings against the domain its parent actually drives. It
returns `(boundaries, CompositionStats)`; the stats (`instances`,
`summarised` — one entry per abstracted instance, `cache_hits`,
`declined`, `boundary_modules` — the distinct module types CDC-008
exempts) make the sharing observable and are what the parity tests
assert against. `cli._summarise_blackboxes` is a thin wrapper that drops
the stats and hands the per-instance boundary map to `find_crossings`. A
`(module, context)` the summariser declines (not provably single-clock —
a genuine multi-clock subtree carrying an internal crossing) is absent
from the map, so its instances fall through to the normal flat walk over
whatever internals the netlist actually contains.

> **Per-instance keying (#257).** The boundary map is keyed by instance
> path, and summarisation is cached by `(module type, clock context)`.
> So a module instantiated N times *in the same clock domain* is still
> analysed once (the cache hit — the epic's "analyse once" goal), while
> the same module type instantiated under *two different* clock domains
> gets a correct per-instance summary for each: each instance's boundary
> crossings are seeded against the domain its parent actually drives.
> This replaces the earlier type-only keying, which could only represent
> one domain per module type.

The end-to-end safety property is **parity**: on a design small enough
to run both ways, the auto-abstracted run (subtree blackboxed +
summarised once) must produce the *same* violations and the same
`summary.*` counts as the fully flattened run, while walking strictly
fewer flops. The `single_clock_leaf_abstract`,
`shared_subtree_compose`, and `foreign_input_no_abstract` fixture pairs
pin this — `shared_subtree_compose` proves a single-clock subtree
instantiated twice is analysed once yet yields identical CDC-004 findings
on the real top-level destination flops, and `foreign_input_no_abstract`
proves a foreign-domain signal driven *into* an abstracted subtree's
input still surfaces its CDC-004 (anchored on the boundary input pin via
`dst_boundary`) — identical to the flattened run.

## 8. The rule pack

`rules.py` is a flat collection of `check_<rule>` functions plus a
small registry:

```python
RULES: dict[str, RuleFn] = {
    "CDC-001": check_cdc_001,
    "CDC-002": check_cdc_002,
    ...
    "CDC-008": check_cdc_008,
    "CDC-009": check_cdc_009,
    "CDC-010": check_cdc_010,
    "CDC-011": check_cdc_011,
}

def run_all(module, crossings, clock_spec, required_depth=2):
    out = []
    for rule_id, rule in RULES.items():
        if rule_id == "CDC-002":
            out.extend(check_cdc_002(module, crossings, clock_spec, required_depth))
        else:
            out.extend(rule(module, crossings, clock_spec))
    return out
```

The CDC-002 special case is the one rule that takes a configuration
parameter (`required_depth`, exposed via `--sync-depth`). The dispatch
pattern is intentionally simple: most rules need nothing beyond the
`(module, crossings, clock_spec)` triple, and the registry grows with
no plumbing churn for new rules.

### 8.1 The shared helpers

Several rules depend on the same structural detectors. These live as
free functions in `rules.py`:

| Helper | Used by | Purpose |
|---|---|---|
| `_sync_chain_depth(module, dst_flop, dst_clock, ...)` | CDC-001, -002, -003 | Walk forward from a flop's Q lane-wise; count the chain length of single-reader same-domain flops |
| `_sync_chain_flops(module, head, ...)` | CDC-005 | Same walk as `_sync_chain_depth`, but returns the ordered tuple of chain flops so callers can reach the chain's tail without re-walking |
| `_backward_fanin(module, start_bits, drivers, ...)` | CDC-006 | Reverse-BFS from a flop's `D` through comb cells; returns the set of upstream flop names and input-port names reached |
| `_forward_reachable_flops(module, start_bits, consumers, ...)` | available for rule writers | Forward analog of `_backward_fanin`: returns the set of *flop* cell names whose `D` is on the cone |
| `_forward_reachable_cells(module, start_bits, consumers, ...)` | CDC-005 | Forward walk that returns *every* cell on the cone — flop or comb. CDC-005 uses this for reconvergence detection so a comb-cell recombination (sync outputs into an OR driving an output port) still counts |
| `_is_multibit_sync_first_stage(...)` | CDC-004 | Verify the destination is a width-N flop whose Q exactly equals another same-domain flop's D lane-wise |
| `_is_gray_encoded_source(...)` | CDC-004 | Backward-walk from src D, looking for the canonical `g = b ^ (b >> 1)` XOR pattern |
| `_is_gated_bus_crossing(...)` | CDC-004 | Recognise handshake-style gating — D updates only when an enable from the dst domain has been synchronized across. Three shapes are accepted: (1) `$mux` directly driving `D` with a dst-domain `S` (the original handshake), (2) the same mux behind up to `_GATING_BUF_BUDGET`=2 transparent fanout buffers (`$buf`/`$_BUF_`/`$_NOT_`/`$not`/`$pos`), (3) destination cell is a `$dffe`-style flop with a dst-domain `EN` fanin |
| `_trace_through_bus_buffers(module, bit, drivers, ...)` | CDC-004 | Walk one D bit backward through up to `_GATING_BUF_BUDGET` transparent single-input buffers and return the surviving upstream driver. Used by `_is_gated_bus_crossing`'s shape-2 path so Yosys-inserted fanout buffers don't hide the originating mux |
| `_clock_network_cells(...)` | CDC-008, -010 | Identify cells whose output transitively drives any flop CLK. CDC-008 *exempts* them ("clock as data" doesn't apply to the distribution itself); CDC-010 *targets* them (a wrong-domain control pin on one of these cells is the failure mode) |
| `_control_pins_for(cell, *, use_heuristic=True)` | CDC-010 | Classify a clock-network cell's *control* pins (those whose transitions can glitch the output clock). Three resolution paths, tried in order: (1) explicit map for Yosys higher-level cells (`$mux.S`, `$dffe.EN`, `$dlatch.EN`) and the gate-level mux family (`$_MUX_` / `$_MUX{4,8,16}_`); (2) prefix paths for the gate-level latch and enable-flop families (`$_DLATCH*` / `$_DFFE_*` / `$_SDFFE_*` → all carry the enable on `E`); (3) a conservative pin-name heuristic — input pins named `E` / `EN` / `CE` / `GATE` / `SE` (case-insensitive) on any other cell are treated as control. `use_heuristic=False` (CLI: `--cdc-010-no-heuristic`) suppresses the heuristic only; the map and prefix paths remain active. Returns the empty set for non-glitch-producing cell types ($buf / $not / AND-trees / vendor cells that lack a heuristic-matching pin) so the rule's outer loop short-circuits |
| `_clock_input_domains_for(module, cell, ctx, clock_spec, control_ports)` | CDC-010 | Set of clock-domain names that drive a clock-network cell's *non-control* inputs. Walks each non-control input backward through combinational cells; records the domain of any flop's Q reached (via `ctx.domains`) and the SDC clock name of any top-level clock port reached directly. Empty set means none of the inputs trace to a classifiable clock — the rule then stays silent (false-negative-biased) |
| `user_sync_flop_names(module)` | CDC-001..-003, -006 | Return cell names of flops the user has annotated `(* cdc_sync *)` |
| `user_gray_flop_names(module)` | CDC-004 | Return cell names of source-side flops the user has annotated `(* cdc_gray *)` |
| `user_static_flop_names(module)` | CDC-001..-004 | Return cell names of source-side flops the user has annotated `(* cdc_static *)` — runtime-constant config/mode registers; suppresses CDC-001..-004 on crossings sourced from the tagged flop |
| `is_ff_cell(cell_type)` (in `flops.py`) | every rule + `find_flops` | Predicate "is this Yosys cell type a flip-flop?" — true for higher-level `$dff*` families AND for gate-level cells whose type starts with one of the `GATE_LEVEL_FF_PREFIXES` (`$_DFF_`, `$_DFFE_`, `$_SDFF_`, `$_SDFFE_`, `$_SDFFCE_`, `$_DFFSR_`, `$_DFFSRE_`, `$_ALDFF_`, `$_ALDFFE_`). The two families have different pin name conventions (CLK vs C); use `flop_clk_pin(cell)` to extract the clock portably. Replaces direct `cell.type in FF_CELL_TYPES` checks across the codebase to make rules work uniformly on pre-`abc` and post-`abc` netlists |
| `flop_clk_pin(cell)` (in `flops.py`) | `find_flops` + `domain.trace_clock_root` | Return the clock-pin connection (tuple of `Bit`s) for a flop cell, portably across families. Higher-level cells use the ``CLK`` pin; gate-level cells use ``C`` |
| `is_latch_cell(cell_type)` (in `flops.py`) | CDC-017 | Predicate "is this a transparent latch?" — true for `$dlatch` (higher-level) and any cell type starting with `$_DLATCH_` (gate-level family) |
| `_has_xor_tail_pulse_recovery(head, head_clock, ctx, module)` | CDC-013 | Positive-recognition phase for the canonical pulse-synchroniser idiom. Walks the dst-domain chain starting at `head`; returns True iff the chain tail's Q is consumed by both (a) a follow-on flop in the same dst clock whose D is exactly tail.Q and (b) an XOR cell (`$xor` / `$_XOR_`) whose two inputs are tail.Q and that follow-on flop's Q. Suppresses CDC-013 on the textbook correct fast-to-slow idiom so the rule doesn't false-fire on a complete pulse-synchroniser (closed by rtl-buddy-cdc#196) |

Each rule is a self-contained function; new rules don't have to learn
the helper APIs unless they need them. Adding a rule is a one-line
edit in `RULES` plus the function definition.

### 8.2 Severity policy

| Severity | Meaning | Drives exit code? |
|---|---|---|
| `error` | High-confidence CDC bug — false-positive risk is low because the structural shape is unambiguous (no synchronizer, comb logic before sync, async reset crossing, …) | Yes (1 if any kept) |
| `warning` | Pattern that might be intentional or might be a bug; the project should review and either fix or waive | Yes (1 if any kept) |
| `info` | Currently unused; reserved for future "structural facts that aren't wrong" reports | No |

A waiver moves a violation out of "kept" and into "suppressed"; only
kept violations drive the exit code.

`--strict` (CLI flag) reframes the kept set without re-gating it:
every `warning` is promoted to `error` before the reporter renders,
so the text banner, JSON `severity`, and SARIF `level` all show
`error`. The exit code is unchanged — any kept violation already
drives 1 — so `--strict` is documentation/UX, not policy. Suppressed
and baseline-carried findings are left at their natural severity
(they aren't driving exit-code outcomes by definition).

### 8.3 Recognition vs. annotation

For each rule the analyzer faces a choice between **structural
recognition** (look at the netlist shape and decide the crossing is
safe) and **user annotation** (trust an `(* attr *)` tag). The two
are not mutually exclusive — both paths exempt the crossing. The
escape-hatch annotation is preferred when:

- the structural detector can't see the relevant shape after flatten
  (e.g. a vendor sync-cell macro replaced with a black-box stub),
- the chain is implemented in a non-canonical way that the detector
  refuses to accept (intentional, to avoid false negatives), or
- the user simply wants to mark the cell as reviewed and move on.

For every rule that uses an attribute, the structural detector is
still primary — the attribute is the relief valve, not the front
door.

### 8.4 CDC-010 — clock-network glitch from a wrong-domain control

CDC-010 is the structural dual of CDC-008. CDC-008 flags a clock
arriving on a *data* pin; CDC-010 flags a *control* pin on a
clock-network cell driven from a foreign clock domain — the case
where the cell is in the right place but the signal that gates it
is not.

The two rules partition the failure mode by which side of the gate
is wrong. They are not redundant: a single bad design can trigger
both, and the docstring on `check_cdc_010` cross-references
`check_cdc_008` so a reader who lands on one rule's hit finds the
sibling check.

Detection composes existing helpers — no new cone-walking
infrastructure:

1. Enumerate clock-network cells with `_clock_network_cells`
   (same set CDC-008 *exempts*).
2. For each cell, look up its control pins via `_control_pins_for`
   — Yosys-primitive only: `$mux.S`, `$dffe.EN`, `$dlatch.EN`.
3. Backward-walk each control pin's fanin with
   `_backward_flop_fanin` (same helper RDC-001 uses on `ARST`).
4. Classify the cell's *non-control* inputs by source clock domain
   via `_clock_input_domains_for`. Both flop-Q sources
   (`ctx.domains`) and direct top-level clock ports
   (`ClockSpec.clock_for_port`) feed the set.
5. Fire when a control-pin fanin flop's domain is asynchronous to
   *every* one of the cell's clock-input domains (per
   `ClockSpec.are_async`). A source flop sharing one of the gated
   clocks is fine; an SDC declaring the pair synchronous via
   `set_clock_groups` suppresses naturally.

Severity is `error`: an async control transition chops the output
clock into runt pulses on every downstream flop, and no
synchronizer at the sink can recover the lost edge.

Cell-shape coverage is three-layered, all gated through
`_control_pins_for`:

1. **Explicit map** — Yosys higher-level cells (`$mux.S`,
   `$dffe.EN`, `$dlatch.EN`, phase 1) and the gate-level mux
   family emitted by `simplemap` / `abc` (`$_MUX_.S`,
   `$_MUX4_.{S,T}`, `$_MUX8_.{S,T,U}`, `$_MUX16_.{S,T,U,V}`).
2. **Prefix paths** for the gate-level latch and enable-flop
   families: any cell type starting with `$_DLATCH`, `$_DFFE_`,
   or `$_SDFFE_` reports `E` as the control pin. Absorbs the
   polarity-/reset-/set-shape variant explosion (`$_DFFE_PP0P_`,
   `$_DFFE_NN1N_`, `$_SDFFE_*`, …) without enumerating every
   combination.
3. **Pin-name heuristic** for tech-mapped library cells: an
   input pin named `E`, `EN`, `CE`, `GATE`, or `SE`
   (case-insensitive) on a cell type outside the map and prefix
   paths is treated as a control pin. Mux-style names (`S` /
   `SEL`) are intentionally *not* in the heuristic set —
   they'd collide with too many non-control pins on unrelated
   cells, so mux shapes have to live in the explicit map.

The heuristic is opt-out via `--cdc-010-no-heuristic` for
libraries whose pin naming conflicts (e.g. a vendor that uses
`EN` for something other than enable). The flag suppresses only
the heuristic path; the explicit map and prefix paths remain
active.

### 8.5 RDC-006 — muxed async reset without local synchroniser

RDC-006 is the structural complement of RDC-005. RDC-005 flags a
multi-source reset converging through combinational AND/OR
without a `$mux`; RDC-006 flags the inverse — a `$mux` *is*
present, source selection is unambiguous, but the selected
reset's deassertion is still asynchronous to the consumer clock.
RDC-005's mux exemption (issue #114) was deliberate: the explicit
selection makes the multi-source pattern intentional, not a
wiring bug. That exemption is correct for RDC-005's failure mode
but silent on the orthogonal one — async deassertion — which
RDC-006 owns.

The two rules partition the muxed-reset failure mode: RDC-005
asks "is the selection explicit?", RDC-006 asks "is the selected
reset synchronised before downstream consumption?". A muxed
reset that immediately reaches a flop's `ARST` triggers RDC-006
but not RDC-005; the same mux feeding a recognised reset-sync
chain triggers neither (the sync chain's own flops are exempted
because the synchroniser is the whole point).

Detection is intentionally narrow in v1:

1. Skip flops in `find_reset_synchronizers(...)` (the chain
   itself is fine; its own ARST is the muxed source by design)
   and flops marked `(* reset_sync *)`.
2. Require `ResetDomain.reset.source == "comb"` and that the
   immediate driver of the reset bit is `$mux` / `$pmux`.
3. Walk the mux's *data* legs (`A`, `B`) — not its select pin
   (`S`) — via `_backward_port_fanin` so the message lists the
   reset sources without conflating the control signal.

Severity is `warning` — the muxed-reset pattern is common in SoC
designs that gate a chip-level reset through a control register,
and the local block may legitimately consume an
upstream-synchronised reset (in which case the user marks the
flop `(* reset_sync *)` or accepts the warning as expected for
their topology). The textbook fix is a 2FF reset synchroniser
between the mux output and downstream consumers; the
`good_derived_async_reset_synced` fixture is the canonical
shape.

The rule's existence also tightened the existing
`good_rdc_005_muxed_reset` fixture: that fixture's original
SV had a muxed reset feeding the data flop's `ARST` directly,
identical to `bad_derived_async_reset_unsync`. Once RDC-006
landed, the fixture was extended to route the mux output through
a local 2FF synchroniser so it stays clean for both rules. The
RDC-005 mux exemption is still exercised — now on the
synchroniser's own flops — and the fixture demonstrates the
real-world correct pattern rather than just "doesn't trigger
RDC-005."

### 8.6 RDC-007 — reset-sync deassertion-polarity check

RDC-007 closes the head-D-constant blind spot in
`find_reset_synchronizers`. The structural recogniser accepts any
chain whose head has a constant `D`, regardless of *which*
constant — but only one of the two valid constants matches the
chain's reset polarity. An active-low reset chain must load
`1'b1` on the deassertion edge (so the chain's Q rises out of
reset); an active-high chain must load `1'b0`. A chain that
loads the *asserted* value instead reloads "in reset" on every
deassertion edge and the synchronised reset never propagates —
worse, the structural recogniser still marks the chain as a
valid synchroniser, silently exempting every downstream consumer
from RDC-001..-006.

Plumbing: `_trace_reset_sync_chain` already finds the head
constant (that's how it knows the chain terminates cleanly); it
now returns both the chain and the constant. A new helper
`iter_reset_sync_chains` enumerates each recognised chain
exactly once (deduplicated by head identity, longest tail wins)
with the head's D constant attached, and RDC-007 walks that list.
`find_reset_synchronizers`'s contract is unchanged.

Severity is `error` — the failure is functional, not stylistic.
A chain that never deasserts is a hard bug; downstream consumers
stay held in reset permanently. Limitation:
`(* reset_sync *)`-marked chains whose head isn't a literal
constant are *not* covered (the structural recogniser has no
constant to compare against). Documented as a known scope
limit; users who need the check on user-marked chains can write
a paired structural assertion.

### 8.7 CDC-019 — independently-synced one-hot decode across CDC

CDC-019 closes the related-lanes blind spot in CDC-004. CDC-004
fires on a multi-bit source flop (WIDTH≥2) crossing without gray
coding or handshake; it can't see the same hazard when the source
is implemented as N parallel WIDTH=1 flops driven by a common
comb decoder. Each source flop is structurally 1-bit, so the
multi-bit-bus detector treats every lane as an independent
single-bit crossing — but the lanes change together at the source
(the comb logic enforces the relationship), and the destination
resolves each via its own sync chain, so transient incoherent
combinations the encoder never emits can appear briefly at the
destination.

Detection walks each `Crossing` whose source is a WIDTH=1 flop,
looks at the cell driving that flop's `D` pin, and groups
crossings by `(driver_cell, dst_clock)`. A group with ≥2
distinct source flops where the driver is a comb cell with ≥2
output bits is flagged. The "≥2 output bits" check is what
distinguishes a shared *decoder* (multi-output, lanes change
together) from a shared *gate* with single-bit fanout to multiple
flops (just normal fan-out, not the failure shape).

Severity `warning` — the pattern is sometimes intentional (the
destination only ever reads one bit at a time, or a separate
handshake gates the dst sample). Suppression composes with the
existing user-vouches attribute set: `(* cdc_gray *)`,
`(* cdc_static *)`, or `(* cdc_sync *)` on *any* of the source
flops in the group suppresses the entire group — one attribute
captures "the user has handled the multi-bit coherence" for the
whole decoder. Suppression when the immediate driver is itself a
flop is structural: chained registers are CDC-001 / CDC-002's
territory.

### 8.8 CDC-021 — flop CLK driven by undeclared port

CDC-021 closes the silent-when-broken methodology gap on the clock-
pin side: a top-level input port that drives a flop's `CLK` but
has no `create_clock` declaration in the SDC. Pairs with CDC-011
(the data-pin equivalent — port reaches `D` without
`set_input_delay -clock`).

The failure mode is silent across the rule pack. An undeclared
port-clock doesn't appear in any `set_clock_groups -asynchronous`
declaration, so `are_async` returns False against every declared
clock, and `_filter_async` drops every crossing involving the
undeclared domain. The downstream rules — CDC-001/002/003 etc. —
never see the affected flops. CDC-021 surfaces the methodology
bug so the user can declare the clock and let the other rules do
their job; it doesn't try to *infer* what the missing clock should
look like.

Detection walks `ctx.flops` directly and consults the precomputed
`declared_clock_ports` set (the union of `clk.ports` across every
declared `Clock`). A flop whose `ctx.domains[name]` is the name of
a top-level input port that *isn't* in that set is flagged.
Generated clocks declared via `[get_pins ...]` have domain names
that are clock names (not port names), so they're filtered out by
the port-membership check. The rule is skipped when no SDC is
supplied (no rules fire in that case anyway).

Severity `error` — undeclared clocks aren't a styling concern;
they disable every other CDC check that touches the domain.

### 8.9 CDC-010 glitchless-clock-mux suppression via SV attribute

The `(* glitchless_clock_mux *)` attribute (aliases
`glitchless_mux`, `glitchfree_clock_mux`) on a clock-mux select
wire instructs CDC-010 to skip the select. Rationale: a
correctly-built glitchless 2-input clock mux (the textbook cross-
coupled-latch envelope, or a foundry library cell) makes an
asynchronous select safe. CDC-010's standard fix advice —
"synchronise the select onto one of the gated clocks" — would
actually break the glitchless property by introducing a single-
clock dependency that defeats the other-clock-aware gating.

Suppression is by *net*, not by source flop: the rule consults a
precomputed `user_glitchless_mux_bits` set (the union of bits in
every netname tagged with one of the attribute aliases) and skips
any control pin whose bits intersect that set. This differs from
the `cdc_sync` / `cdc_gray` / `cdc_static` attributes (which are
flop-name sets) because the safe-handoff promise is about the
*wire* (the select net), not about the producing flop — a select
sourced from a top-level port and a select sourced from a foreign-
domain flop both need the same suppression.

Out of scope for this attribute: structural detection of the
cross-coupled-latch shape itself (so the user doesn't have to
attach the attribute). That's a separate followup; the attribute
path is the right shipping unit because it gives the user an
explicit escape hatch even when the mux is in a library cell
(no SV source to annotate inside).

### 8.10 CDC-020 — sliced-bus reconvergence across CDC

CDC-020 closes the *multi-bit-source-sliced-into-1-bit-lanes*
blind spot in CDC-004. `find_crossings` emits one `Crossing` per
`(src_flop, dst_flop)` pair with `width = number_of_bits_landing
_at_that_specific_dst`. When a 4-bit source flop is sliced via
wire assignments and each bit lands at a separate dst flop, every
per-lane crossing has `width = 1` and CDC-004's `width <= 1` skip
drops them all. The source is genuinely multi-bit; the lanes
change together at the source register; the dst-side per-lane
sync chains resolve metastability on their own schedule, so
transient incoherent combinations can appear at the destination.

Sibling of CDC-019 (rtl-buddy-cdc#204): same per-lane-independent-
sync hazard, but the source is a true multi-bit register rather
than a shared combinational decoder.

Detection walks `crossings` looking for src flops with `len(q) >=
2`, groups by `(src_flop, dst_clock)`, and fires when the group
contains ≥2 distinct `dst_flop`s. Suppression mirrors CDC-004:

* `(* cdc_gray *)` on the source flop,
* `(* cdc_static *)` on the source flop, or
* structural gray encoding (`_is_gray_encoded_source`) combined
  with *any* per-lane destination being a multi-bit-sync first
  stage (`_is_multibit_sync_first_stage`).

Severity `warning` — sometimes intentional (the destination only
reads one bit at a time, or a separate handshake gates the
sample).

### 8.11 CDC-018 — cascaded synchroniser smell

CDC-018 is a *quality-of-life* check: surface CDC crossings whose
destination sync chain depth exceeds the textbook 2FF minimum by a
clear margin. Classic patterns:

* two engineers each add their own sync chain on the same wire,
* a refactor leaves the original chain in place when a new wrapper
  is added,
* a designer "adds a stage for safety" without realising the
  underlying physics doesn't reward depth beyond 2 (a third stage
  helps MTBF only at the noise floor; a fourth is irrelevant).

The chain still works — extra latency, slightly worse MTBF tail —
so the severity is `warning`, not `error`. The rule's job is to
make the depth visible during review, not to force a fix.

Detection: walk each CDC crossing's dst flop through the existing
`_sync_chain_depth` helper. That helper's same-domain single-reader
walker terminates correctly when the chain's value is consumed by
anything other than a follow-on flop D, so a chain whose tail
feeds two registers (legitimate fanout) doesn't trip the rule.
Group findings by `(src_flop, dst_clock)` and emit one finding per
group (the deepest chain in the group wins) to avoid multi-firing
on sliced buses.

Threshold defaults to 4 — chains of depth 2 or 3 stay silent (the
textbook 2FF sync plus an MTBF-friendly 3-stage variant). The
threshold is configurable via `--cdc-018-depth-threshold` (CLI)
and `run_all(..., cdc_018_depth_threshold=N)` (programmatic).
Designs that intentionally run deeper chains can raise it; the
chain head can also be marked `(* cdc_sync *)` to suppress
unconditionally.

### 8.12 G-5 handshake reporter refinement

CDC-012 (gated bus crossing without synced-back ack) and CDC-001 /
CDC-002 (missing 2FF sync on a single-bit crossing) can both fire
on the same async domain pair — they are two views of the same
incomplete-handshake protocol. Today the user sees them as
unrelated findings; the G-5 refinement links them with a one-line
``[handshake-related]`` tag appended to the CDC-001 / CDC-002
message.

Implementation: a final post-processing pass in ``run_all``
(`_tag_handshake_related`). For every CDC-012 finding, record its
``(src_clock, dst_clock)`` pair; for every CDC-001 / CDC-002
finding whose ``crossing.{src,dst}_clock`` matches that pair in
either direction, replace the violation with a copy whose
``message`` carries the tag. The detection logic for CDC-001 /
CDC-002 / CDC-012 themselves is unchanged — no firing-set delta,
no new violations, just enriched message text on the linked
findings.

This stays close to the spec's "rule pack is a chain of pure
functions" rule: the refinement is *another* pure function over
the violations list, layered on at the end of ``run_all``.

### 8.13 RDC-008 — unsynced primary-reset-port deassertion

RDC-008 fills the port-sourced gap in the RDC family. RDC-001
catches reset crossings whose source is a *flop's* Q in a foreign
clock domain; it deliberately doesn't fire when the source is a
top-level reset port directly wired to consumer flops' `ARST`
pins. Both shapes have the same recovery/removal hazard
(deassertion unsynchronised to the consumer clock); only the
source classification differs.

**Asymmetric-intent detection.** RDC-008 only fires when:

1. The port has consumers in some clock domain that *already* has
   a recognised reset-synchroniser chain — proof the user knows
   the port needs synchronisation, and
2. The same port drives ≥2 consumer flops in a *different* clock
   domain *without* a sync chain there.

The second clock domain is where the methodology bug lives. The
first criterion (chain exists elsewhere for the same port) gates
out "I just used the raw port everywhere" simplifications common
in small hand-authored RTL — those are a different concern than
RDC-008 is calibrated for. The second criterion (≥2 unsynced
consumers) gates out single-flop shortcut uses, which often reflect
designer judgement on a specific timing path rather than a
distribution-pattern bug.

This makes RDC-008 narrower than the strictest reading of the rule
("every port-sourced ARST needs a chain") but the noise/signal
ratio under the strict reading was untenable on the existing test
corpus. Future revisions can widen via an opt-in flag.

Severity `error` — methodology bug; the chain's absence is the
root cause of a class of intermittent silicon issues.

### 8.14 SDC clock-graph validation (G-11)

`validate_clock_graph(spec)` is the cross-statement counterpart to
the per-command `_handle_*` parsers in `sdc.py`. Where the latter
catch line-local issues (missing `-name`, bare `-filter` clauses),
the validator looks at the *collective* clock graph after every
statement has been parsed and surfaces shapes that individually-
valid statements compose into an inconsistent or undefined whole.

Three checks:

1. **Same port in multiple clocks**: scan `Clock.ports` across every
   declared clock; flag any port that appears in ≥2 clocks.
   `clock_for_port` is deterministic (first match wins) but the
   user's intent is ambiguous.

2. **Unresolved master**: for each generated clock with
   `Clock.master` set, check the master name exists in
   `spec.clocks`. An unresolved master makes `ClockSpec.resolve`
   return the unknown name unchanged, breaking `are_async`'s
   root-comparison.

3. **Master cycle**: walk each generated clock's master chain with
   a per-walk visited list; if a name is revisited, the chain is
   cyclic. `ClockSpec.resolve` has a cycle guard, but the SDC is
   methodology-broken.

Duplicate clock names are caught inline by the per-command
handlers (`_handle_create_clock` / `_handle_create_generated_clock`):
they check `name in spec.clocks` before assignment and append a
warning if so. Inline detection has both sides visible — cheaper
than re-deriving from the final spec.

All diagnostics flow through `spec.partial_warnings` so they reach
the user via the existing text-format warning channel.
JSON/SARIF promotion to proper `Violation` records is a deferred
followup; this PR keeps the linter scoped to its low-risk surface.

### 8.15 CDC-013 — fast-to-slow control-event loss on a toggle sync

CDC-013 is the structural complement of CDC-009. CDC-009 owns the
raw-pulse case (`D = A & ~A_d` edge detector — narrow src pulse
that may slip between dst rising edges); CDC-013 owns the toggle
case (`D = en ? ~Q : Q` toggle-with-enable — two src events
between dst samples that cancel to zero edges at the destination).
Both fire under the same fast-to-slow clock ratio
(`src_period × PULSE_FACTOR < dst_period`), but they classify
different `D`-pin shapes and so cannot both fire on the same
crossing.

The classifier lives in `rtl_buddy_cdc.pulse` alongside CDC-009's
edge-detector classifier. `classify_toggle_d_pin` matches the
Yosys-synthesised shape of `always_ff if (en) q <= ~q;`: a `$mux`
whose `Y` is the src flop's `D`, whose data pins (`A`, `B`) are
`(Q, ~Q)` in either order (Yosys picks the ordering based on the
condition polarity), and whose `S` is the load-enable (unconstrained
by the classifier — the rule body doesn't care what the enable is).
The classifier is shape-only and returns `"other"` for handshake /
counter / pulse-stretcher patterns whose `D` is a priority-encoded
mux nest, an adder, or a reduction — those naturally fall outside
the toggle shape and stay silent without needing an explicit
exemption.

Severity is `warning`. Many designs use the toggle synchroniser
pattern correctly by guaranteeing inter-event spacing at the
application level (or by gating event re-arming on a downstream
backpressure signal). The rule invites review rather than declaring
an unambiguous bug; the textbook fix is a req/ack handshake (source
holds value until ack returns, synced back through a 2FF) or an
event counter with backpressure, both of which produce a non-toggle
`D` and silence the rule structurally.

### 8.16 CDC-012 — functional data-hold on a gated multi-bit crossing

CDC-012 layers a functional check on top of CDC-004's structural
gated-bus exemption. CDC-004 accepts a multi-bit crossing whose
destination only latches data when a destination-domain enable
allows it (mux-on-D with sync'd select, or `$dffe` with sync'd
`EN`). That structural shape ensures the destination samples on a
clean enable but does not ensure that the source payload is stable
across the enable's sync-chain latency. CDC-012 flags the gap.

The bug pattern (canonical in `bad_functional_datahold_enable`):
source registers a new payload every src cycle, asserts a request,
the request propagates through a 2FF synchroniser into the
destination, and the destination latches the payload it sees *N*
dst cycles after the original request. By that time the source
payload may have advanced. The destination captures an incoherent
value — not the payload that motivated the original request.

The textbook fix is a req/ack handshake: source holds the payload
(and the request) until a synced-back ack proves the destination
has sampled. The handshake's structural marker — and the rule's
detection signal — is a src-clock flop with `D`-pin fanin from a
dst-clock flop's `Q`: the `ack_sync` register. CDC-012 stays
silent whenever that feedback path is reachable from *this
crossing's* source flop.

The detection is per-crossing:

1. Multi-bit (`width > 1`) async crossing with a non-trivial src
   flop.
2. Crossing passes `_is_gated_bus_crossing` (so CDC-004 is silent
   — CDC-012 does not duplicate CDC-004's territory on
   un-gated buses).
3. Source not gray-encoded (structural `g = b ^ (b >> 1)` detector
   via `_is_gray_encoded_source`, or `(* cdc_gray *)` annotation
   via `ctx.user_grays`). Gray-coded sources guarantee at most one
   bit changes per src cycle, so any dst-side sample is coherent
   regardless of the enable's sync-chain latency.
4. The crossing's source flop has no register-neighbourhood path
   back to a `dst_clock` flop (`_has_dst_to_src_feedback`).

`_has_dst_to_src_feedback` walks the register-neighbourhood of
`crossing.src_flop`: from the payload register it hops flop → its
full input fanin (`D` plus enable / set / reset, via
`_flop_input_fanin_bits`) → flop, staying inside the src domain,
and reports feedback the moment the walk reaches a dst-domain flop
(bounded to a handful of flop hops — enough to clear a 2–3FF ack
synchroniser plus the enable flop(s) gating the payload load). The
feedback result is cached per `c.src_flop.cell.name`, one entry
per source flop.

Feedback presence is a **crossing-level** property, not a
domain-level one. Scoping the walk to the crossing's own source
flop is what stops an unrelated handshake (or a FIFO's pointer
sync) between the same domain pair from silencing a genuinely
broken crossing. The earlier implementation cached the predicate
on the `(src_clock, dst_clock)` pair and short-circuited *every*
gated crossing between the clocks on the first feedback hit — a
module with one proper handshake alongside one broken req-only
crossing silenced the broken one (rtl-buddy-cdc#239, surfaced by
the #238 grammar gap-mining run). The paired fixtures
`bad_mixed_handshake_datahold` / `good_mixed_handshake_datahold`
pin both directions.

Severity is `warning` — the rule's structural heuristic for
"handshake present" can't see application-level guarantees (e.g. a
slow-write config-register bus where the host writes once and
waits many src cycles), which correctly trip the rule. `warning`
invites review rather than declaring an unambiguous bug.

CDC-012's landing also tightened three CDC-004 positive fixtures
(`good_dffe_gated_bus_crossing`, `good_buffered_gated_bus_crossing`,
`good_unconstrained_input_bus_two_domains_typed`). Their original
SV demonstrated only the destination-side gating shape, with no
source-side handshake — exactly the pattern CDC-012 catches. Each
was extended with a synced-back ack and source-held payload so
they now demonstrate the real-world correct pattern rather than
just "doesn't trigger CDC-004."

## 9. Waivers

`waivers.py` implements the smallest waiver-file format that scales
to a real project. One statement per line:

```
waive <RULE-ID|*> <regex> [reason ...]
```

The matcher tries the regex against three strings per violation in
"most specific → least" order:

1. The violation's `cell_name`.
2. The canonical `"src_flop -> dst_flop"` text (when there's a
   crossing).
3. The violation's `message`.

A hit on any of the three suppresses. The first matching waiver
wins, mirroring user expectation that waiver files are read top-
down.

Suppressed findings are kept in the report (with the matching reason
and waiver-line number), not silently dropped. They appear in JSON
output and in SARIF as `suppressions`. They don't drive the exit
code, so a fully-waived run returns 0.

This deliberately mimics the Spyglass `.swl` workflow at a much
smaller surface — no scope qualifiers, no severity overrides, no
expiry dates. Add those when a real project asks for them, not
preemptively.

### 9.1 Baseline filtering (`--baseline`)

`--baseline FILE.json` is auto-derived waivers. The flag points at a
prior JSON report (the same shape `render_json` emits); findings
whose `(rule_id, cell_name, message)` tuple matches an entry in the
baseline's `violations` or `baseline_carryover` lists are filtered
out of the kept set and moved into a third bucket on
`AnalysisResult.baseline_carryover`. The kept-vs-suppressed-vs-
carryover partition is performed in `cli._analyze_module_and_report`
after waivers and before `--strict` promotion (so a carryover
warning isn't promoted; carryover findings never drive exit code).

JSON output gains `summary.baseline_carryover` (int) and a top-level
`baseline_carryover` list of `_violation_to_dict` entries; SARIF
emits each carryover entry with a `suppressions` field whose
`justification` is `"carried over from baseline"` (distinguishing it
from waiver-suppressed entries). The match key reuses fields the
JSON schema already exposes; `cell_name` is now part of every
violation dict for this purpose.

Baseline files chain — a finding already in the baseline's
`baseline_carryover` list stays carried over on the next run too —
so re-baselining doesn't re-flag inherited findings.

## 10. Reporting

Three formatters share the same `AnalysisResult` input:

- **`render_text`** — human-readable. Module summary, domain counts,
  crossing list, then the violations grouped by rule. Inside each
  rule group, findings are bucketed by `instance_path` (§4.6) under
  per-instance headers (`[top]` for top-level findings,
  `u_block_a / u_sync` for nested paths). The bucketing collapses
  to a flat layout when every finding in the rule group lives at
  top, so flat IP-block fixtures' output is byte-identical to
  pre-hierarchical-reporting. Designed for terminal and CI-log
  review.
- **`render_json`** — full structured output. Stable schema. Used by
  `rb cdc` (rtl-buddy's wrapper) to extract violation counts, and by
  any custom dashboard. Every violation / suppressed /
  baseline-carryover entry carries `instance_path: list[str]`
  (always present, `[]` at top, never `null` or missing). A
  top-level `by_instance` list aggregates kept violations by path,
  sorted lexicographically (empty path first), with per-rule counts.
  Suppressed and baseline-carried findings are excluded from
  `by_instance` — the aggregation answers "what's actually failing
  per block", not "what structural findings exist". The
  `JSON_CONTRACT` keys (`summary.violations`, `summary.suppressed`,
  `summary.crossings`) and the `--baseline` match key
  (`rule_id`, `cell_name`, `message`) are unchanged by the
  hierarchical additions, so historical baselines do not re-flag
  after the bump.
- **`render_sarif`** — SARIF 2.1.0, GitHub-Code-Scanning-compatible.
  Populates `tool.driver.rules` for every rule that fired in the
  run. Each result carries `physicalLocation.region` parsed from the
  cell's `attributes["src"]`, and — when the violation's
  `instance_path` is non-empty — a `logicalLocations` entry with
  `name` (leaf component), `fullyQualifiedName` (dot-joined path),
  and `kind: "module"`. `logicalLocations` is omitted (not emitted
  as an empty array) at top instance so the output diff stays
  minimal on flat fixtures. Suppressed findings are emitted with a
  `suppressions` field so the alert exists but doesn't fail the
  build.

Format selection is purely a CLI flag (`--format text|json|sarif`);
the analyzer pipeline runs the same way regardless.

A separate, additive output stream is the **domain-map artifact**,
emitted as a sidecar JSON file by `--emit-domain-map FILE.json` and
implemented in `rtl_buddy_cdc.domain_map.build_domain_map`. The map
captures the analyzer's clock-domain view independently of the
findings stream: clocks, generated clocks, async / exclusive groups,
false-path pairs, per-flop domain assignments with source locations,
the typed port→clock map, and structural crossings (each tagged with
`async_per_sdc` so consumers know which subset would reach the rule
pack). It is its own contract: `schema_version: "1.0"` is pinned by
`domain_map.SCHEMA_VERSION`, breaking changes require a bump, and the
artifact ships sorted deterministically so consumers can golden-diff
against the same inputs. `--no-findings` short-circuits rule
evaluation when the map is the sole deliverable. Primary consumer
today is [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view).

Both `instance_path` (synth-leaf form) and `source_instance_path`
(deepest enclosing SystemVerilog-source module instance, rooted at
the design top) are emitted on every `flop_domains[]` entry; each
`crossings[]` entry mirrors them with `dst_source_instance_path`
and — when the source endpoint is a flop — `src_source_instance_path`.
These are the additive fields from issue #136: consumers map the map
back onto source-level hierarchy without having to re-derive the
chain by stripping `$slang$…`/`$procdff$…` leaves themselves. The
fields are emitted as `null` (kept in the dict, never omitted) when
the analyzer can't resolve the chain, so a `null` is distinct from
an older producer that didn't emit the field at all. With today's
Yosys (`proc; flatten`) and slang frontends the chain is always
resolvable. Yosys runs that skip `proc`/`flatten` are out of scope —
the analyzer assumes a flattened netlist.

A parallel sidecar — the **reset-domain-map artifact** — is emitted
by `--emit-reset-domain-map FILE.json` and implemented in
`rtl_buddy_cdc.reset_domain_map.build_reset_domain_map`. Same
contract shape (own `schema_version`, deterministic sort, additive
backward-compatible evolution) but a different payload: distinct
upstream reset sources (port / inferred / constant), the recognised
reset-synchroniser stages, per-flop reset assignments, and structural
reset crossings (kinds `async-deassert`, `polarity-mismatch`,
`sync-crossing`, `comb-driven`). The two maps are intentionally
separate artefacts — clock and reset analyses evolve on different
schedules, and consumers enable each overlay independently. Both
flags compose in a single run; the shared `design.top` /
`design.frontend` envelope blocks let consumers join the two
artefacts safely. See
[`rtl-buddy-cdc-reset-domain-map-schema.md`](rtl-buddy-cdc-reset-domain-map-schema.md)
for the field reference and
[`rtl-buddy-cdc-reset-domain-analysis.md`](rtl-buddy-cdc-reset-domain-analysis.md)
for the producer-side pipeline (data model, source classification,
synchroniser recogniser, and the RDC rule family built on top).

The SARIF output is validated against the OASIS-published 2.1.0
schema by `tests/test_sarif_schema.py`, vendored at
`tests/schemas/sarif-2.1.0.json` so CI does not depend on
schemastore reachability. Five render paths are covered (clean,
with-violations, waiver-suppressed, baseline-carryover, plus a
rule-shape variant pass). The schema test catches the structural
mistakes the shape-assertion tests miss — typo'd field names,
wrong-type values, required-sub-field omissions on code paths no
shape assertion happens to read.

## 11. Extension points

Where the analyzer is designed to be extended without architectural
churn:

- **New rules.** Add a `check_<rule>` function in `rules.py` and one
  line in `RULES`. Helpers in §8.1 are usable by new rules. Severity
  is the rule's own choice.
- **New SV attributes.** Define a frozenset (next to `USER_SYNC_ATTRS`
  / `USER_GRAY_ATTRS`) and a `user_<x>_flop_names(module)` helper;
  consult it in the rules that should honor the annotation.
- **New SDC commands.** Add a handler in `sdc.py` and extend
  `ClockSpec` if a new piece of state is needed. The SDC plan
  (§6.3 + the Phase-1 plan in the project docs) is the reference for
  how the next four are landed.
- **New output formats.** Add a `render_<fmt>` function in
  `reporter.py` that takes `AnalysisResult` and writes to a text
  file. Wire it into `OutputFormat` and the dispatch in
  `cli._analyze_and_report`.
- **New clock-network shapes.** Add the cell-type set to one of the
  category constants in `domain.py` (`_BUFFER_TYPES`, `_GATE_TYPES`,
  `_MUX_TYPES`) — the walker is data-driven from those.
- **New elaboration frontends.** Add a submodule under `frontends/`
  exposing `elaborate(sources, top, **kw) -> Module`, register it in
  the `Frontend` enum in `frontend.py`, and dispatch in
  `frontend.elaborate`. The frontend must produce the Yosys-style
  `Module` contract documented in [§3.1](#31-frontend-layer); the
  rule pack consumes that contract regardless of source.

Where the analyzer is **not** trying to be extensible:

- Hierarchy: today's pipeline assumes flatten. Hierarchical analysis
  is a major design change, not an extension point.

## 12. Integration with rtl-buddy

`rtl_buddy` (the broader CLI) owns Yosys invocation, model resolution,
and result aggregation. `rtl-buddy-cdc` is invoked as a subprocess
through `tools/cdc_rtl_buddy.py`, which:

1. Resolves the model's filelist (via the same `VlogFilelist` plumbing
   used by `rb synth`).
2. Calls `rtl-buddy-cdc lint` once with `--format text` and once with
   `--format json` — the JSON is parsed for the summary counts that
   feed `CdcResults`; the text is kept as a human-readable artefact
   alongside the JSON.
3. Forwards `--sync-depth` (from `cfg-cdc-tools.opts.sync-depth`) and
   `--waivers` when configured.

The subprocess boundary is intentional: it lets `rtl_buddy` pick up
new analyzer releases via `uv sync` without code changes in the
wrapper, and lets the analyzer evolve its Python API without
breaking the integration. The trade-off is that startup cost is paid
twice per analysis (text + JSON); the wrapper documents this and
notes the path to a single-invocation refactor if it becomes a
hotspot.

See [rtl-buddy-project-template](https://github.com/rtl-buddy/rtl-buddy-project-template/tree/main/lint/cdc)
for working `cdc.yaml` and `cdc_regression.yaml` examples.

## 13. Performance characteristics

The analyzer is designed for **interactive run times on individual IP
blocks**, not full-SoC scale. On the alu_accel design (~90 flops, ~545
cells, post-flatten) a full lint run is well under one second. The
current bottlenecks if scaled up:

- `find_crossings` is `O(F · max_hops · avg_fanout)` where F is flop
  count. The hop budget caps the worst case, but very wide datapaths
  with shallow logic still produce a large frontier.
- `trace_clock_root` is called once per flop. `_bit_drivers` is
  precomputed once and shared.
- The rule pack is `O(C)` in crossings for most rules, with two
  exceptions: CDC-005 (reconvergence detection) is `O(C²)` worst-
  case from the per-group cone walks (each chain's
  `_forward_reachable_cells` traversal is `O(cells + nets)`
  bounded by `max_depth=12`), and CDC-008's clock-network
  identification walks the cell graph from every flop CLK. The
  `bit_consumers` index is precomputed once per `run_all`, so the
  cone walks reuse a shared O(cells × pins) builder cost.

Hierarchical mode (deferred) is the architectural change that would
let the tool scale to SoC-level designs by analyzing each flatten
unit independently and stitching the results.

## 14. Testing strategy

Each rule has at least one **bad fixture** (designed to fire it) and
one **good fixture** (the textbook fix that must not fire). Fixtures
are paired SystemVerilog + SDC + pre-built Yosys JSON under
`tests/fixtures/`. The pairing is what catches false positives if a
rule gets tightened — the good fixture acts as the regression net.

In addition:

- `tests/test_good_fixtures.py` is a parametrized sweep over all
  good fixtures asserting zero violations.
- `tests/test_bad_*.py` is one file per bad fixture asserting the
  expected rule fires and no other rule false-fires.
- `tests/test_waivers.py` covers waiver parsing and matching.
- `tests/test_sdc.py` covers the parser corners.
- `tests/test_reporter.py` covers each output format's contract.

The pre-built JSON fixtures are committed so the test suite doesn't
require Yosys to be installed (the wrapper integration tests do, but
the unit suite does not).

## 15. Grammar fuzzer (Stage 4)

Stage 4 (rtl-buddy-cdc#222) sits above the hand-authored corpus
and the Stage-3 xeno mutator (Layer B of rtl-buddy-cdc#221): a
**grammar** that emits novel topologies the corpus hasn't seen, on
the same `RenderedCase` → Yosys / slang → analyzer pipeline the
template-driven fuzz uses. A grammar-derived `.sv` is just another
input to `tests.fuzz.runner.run_case` — the Yosys cache, the slang
cache, the rule pack, and the analyzer-differential oracle all
treat it identically to a template case.

The grammar lives under `tests/fuzz/grammar/`:

- `core.py` — terminal / non-terminal types, composition, top-level
  driver.
- `productions.py` — concrete productions (the registered
  non-terminals).
- `steering.py` — coverage-steering picker.

### 15.1 Terminals

The grammar's leaf alphabet — the things production bodies stitch
into rendered SV:

| Terminal       | Module                          | Notes                                                                          |
| -------------- | ------------------------------- | ------------------------------------------------------------------------------ |
| `ClockDomain`  | `grammar.core.ClockDomain`      | Name + period (ns). One `create_clock` per declared domain.                    |
| `Port`         | `grammar.core.Port`             | Top-level module port. Inputs with a `sampling_clock` get a `set_input_delay`. |
| Flop kind      | (open-coded inside productions) | Sync / async-reset / gated forms picked per-production.                        |
| Comb cells     | (open-coded inside productions) | E.g. `wire comb = a & b` in `_emit_comb_source`.                               |
| SV attributes  | (open-coded inside productions) | E.g. `(* cdc_gray *)` on the gray-counter production's net decls.              |
| SDC clauses    | `grammar.core._render_sdc`      | `create_clock`, `set_clock_groups -asynchronous`, `set_input_delay`.           |

Flop kinds, comb cells, and SV attributes are open-coded inside
each production today rather than promoted to typed terminals.
Promotion is a follow-up if a future production needs to *parametrise*
over them (e.g. a flop-kind sweep) — until then the per-production
literal SV stays the cheapest representation.

### 15.2 Non-terminals (productions)

A `Production` is `(name, emit, declared)`. `emit(ctx) -> Fragment`
generates the SV bits; `declared: Prediction` is the production's
static verdict — the rule ids it claims to lift, mirroring xeno's
`Prediction.cdc_rules_added` / `cdc_rules_removed` shape so the
coverage-steering picker can reason about productions and mutant
operators uniformly.

| Production              | Declared `cdc_rules_added` | Non-terminal class issue #222 calls out |
| ----------------------- | -------------------------- | --------------------------------------- |
| `clean_sync_chain`      | (empty)                    | sync chain (clean reference)            |
| `unsynced_single_bit`   | `{CDC-001}`                | sync chain (negative — depth=0)         |
| `comb_source`           | `{CDC-006}`                | comb source                             |
| `gray_counter_crossing` | (empty)                    | gray counter                            |
| `missing_reset_sync`    | `{RDC-001}`                | reset-sync chain                        |
| `handshake_req_ack`     | (empty)                    | handshake req/ack pair (clean)          |
| `handshake_no_ack`      | `{CDC-012}`                | handshake req/ack pair (negative)       |
| `fifo_skeleton`         | (empty)                    | FIFO read/write skeleton                |

`mux tree` is the one remaining non-terminal class from the
issue's Sketch — deferred because CDC-010's SDC shape (two
physical clock ports sharing one logical clock name via
`create_clock [get_ports {a b}]`) needs the SDC emitter to grow
multi-port-per-clock support, which is a follow-up.

### 15.3 Composition

`compose(productions, ctx)` is the driver. Each production is
emitted independently — there is no signal-threading between
productions, so a case with two productions becomes one module
with two parallel crossing sites. The composed `Fragment` carries
the union of all production SV (decls + always_blocks + assigns +
ports + clocks), and the verdict is `Prediction.merge`-folded
across the chosen productions.

`Prediction.merge` is *removed-wins*: a production introducing a
finding (e.g. an unsynced crossing → CDC-001) combined with one
silencing it (e.g. a hypothetical `cdc_sync_attribute_blanket`
that strips the rule globally) leaves the rule out of the combined
`cdc_rules_added`. Today's productions don't use `cdc_rules_removed`
— union math is trivially additive — but the merge contract is
fixed so future silencing productions don't have to re-design the
composition rule.

### 15.4 Generation

`generate(seed)` is the public entry point. Deterministic given
the seed (and the production registry) — same seed always
produces byte-identical SV / SDC / params. This is the
reproducibility property issue #222 Sketch point 4 calls out;
`tests/fuzz/test_grammar.py::test_generate_is_deterministic_for_seed`
pins it.

Defaults: `n_productions = rng.randint(2, 4)`. Productions are
sampled uniformly from the registry. Coverage steering (next
section) passes a filtered subset to bias generation.

### 15.5 Coverage steering

`grammar.steering` is the loop that closes corpus growth ↔ coverage
gain. Two functions:

- `under_covered_rules(fires, threshold, rule_universe=None)` —
  given a per-rule fires counter and a threshold, return the
  rules whose fire count is below it. Pass `rule_universe =
  set(rules.RULES)` to surface zero-fire rules too.
- `productions_lifting(rule_ids)` — given a set of rules, return
  the productions whose `declared.cdc_rules_added` intersects
  them. Pass the result back to `generate(productions=...)` to
  bias the picker.

The `tests/fuzz/coverage.py` report surfaces *steerable rules*:
rules with zero fires in the uniform-pass grammar column but at
least one production declaring them. That's the actionable
signal — re-run a steered batch to lift them.

### 15.6 Adding a non-terminal

A future engineer wanting to add (e.g.) a credit-based
flow-control wrapper:

1. Add a private `_emit_credit_handshake(ctx) -> Fragment`
   function in `productions.py`. The function builds SV strings
   via `ctx.uniq("credit")` for unique signal names, declares
   any new ports / clocks it needs, and packs them into a
   `Fragment` alongside its `Prediction` delta.
2. Append a `Production("credit_handshake", _emit_credit_handshake,
   declared=Prediction(cdc_rules_added=frozenset({...})))` row
   to the `PRODUCTIONS` tuple.
3. The coverage report's steering hints will surface the new
   production's declared rules automatically; no change to the
   report or the steering picker is required. The directional
   check in `tests/fuzz/test_grammar.py::test_predictions_directional`
   parametrises over `PRODUCTIONS`, so the new production gets
   its own per-rule assertion for free.

### 15.7 Cross-frontend differential

Grammar cases run through the same Yosys / slang oracle the
hand-authored corpus uses (Stage-3 Layer C of rtl-buddy-cdc#221).
The grammar-side test lives in `tests/fuzz/test_grammar_diff.py`
under the `fuzz_grammar` marker (not `fuzz_diff`) so the grammar
selection can be sized independently as the production registry
grows.

### 15.8 Rate calibration

Closes done-when criterion 1 of rtl-buddy-cdc#222 ("Grammar emits
≥N novel topologies / minute on a single core"). The bench script
lives at `scripts/bench_grammar_rate.py`; invoke with
`uv run python -m scripts.bench_grammar_rate`.

Baseline (single core, Apple M2 Pro, Python 3.13, 20 s per phase,
2026-05-29):

| Phase                              | Cases/min   | Notes                                    |
| ---------------------------------- | ----------- | ---------------------------------------- |
| `emit-only` (`generate()` alone)   | ~1,800,000  | Pure-Python; dominated by RNG draws.     |
| `elaborated` (+ yosys + analyzer)  | ~6,500      | Cache-bypassed; the operational cost.    |

Both figures comfortably clear any plausible ≥N threshold —
`elaborated` is the rate that bounds a mining run; 6.5k/min/core
means the 32-seed coverage report's grammar pass adds ~0.3 s and
a bounded mining run of 10k seeds completes in ~95 s on one core.

Re-run on different hardware (or after a Yosys / pyslang version
bump) by invoking the bench script; the table here gets updated
when the rate moves materially (≥10%).

### 15.9 Gap mining

Operationalises done-when criterion 2 of rtl-buddy-cdc#222
("Generated corpus surfaces ≥1 new gap candidate"). Script:
`scripts/gap_mining.py`. Invoke with
`uv run python -m scripts.gap_mining --seeds N`.

Two signals are reported per seed:

- **Surprise** — a rule fired that no chosen production declared.
  Known co-fires (documented in `_KNOWN_COFIRES` inside the
  script) are suppressed so the report focuses on novel patterns.
- **Missing** — a rule was declared by some chosen production but
  didn't fire. The false-negative axis: either a production lies
  about its verdict, or the analyzer has a gap.

A persistent, high-frequency surprise *or* missing pattern is the
actionable signal — file a gap candidate against rtl-buddy-cdc.
The PR that runs the bounded mining session is the place where
the findings get triaged; the issue body holds the analysis (see
AGENTS.md "Design proposals live on GitHub").

Baseline 1000-seed bounded run (Apple M2 Pro, 2026-05-29, on the
eight-production registry):

| Signal   | Cases | Pattern        | Frequency |
| -------- | ----- | -------------- | --------- |
| Surprise | 0     | (none)         | 0%        |
| Missing  | 170   | `{CDC-012}`    | 17%       |

The CDC-012-missing pattern was the gap candidate from this round.
Root cause: `check_cdc_012`'s feedback-presence check cached per
`(src_clock, dst_clock)` *domain pair*, not per *crossing*. Any
production that introduced a dst→src structural feedback in the
same domain pair (e.g. `handshake_req_ack`'s ack-sync chain,
`fifo_skeleton`'s rptr_gray sync) silenced CDC-012 on every other
gated multi-bit crossing in the same domain pair, including
unrelated ones. Fixed in rtl-buddy-cdc#239 by scoping the feedback
search to the crossing's own source flop (see §8.16); a
re-run of the 1000-seed bounded mining session reports `Missing: 0`.
