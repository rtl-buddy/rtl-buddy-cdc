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
        │ liveness.prune_dead_cells (1a)   │  drop cells no output can observe
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
   ─── // rbcdc: ────▶ │ waivers.apply       (8)          │  partition into kept / suppressed
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
| `regroup.py` | Loader normalisation: rebuild bit-blasted gate-level registers (and the lane-wise logic in front of them) as wide coarse cells (rtl-buddy-cdc#337) | `regroup_flops`, `REGROUPED_ATTR`, `ORPHAN_ATTR` |
| `gate_eval.py` | Truth tables of small combinational cones (functional gray recognition on gate netlists) | `cone_tables` |
| `flops.py` | Recognise the 11 Yosys FF cell variants and extract CLK / D / Q | `Flop`, `FF_CELL_TYPES` |
| `domain.py` | Trace clock roots, find flop→flop crossings | `FlopDomain`, `Crossing`, `trace_clock_root`, `find_crossings` |
| `memory.py` | Decode inferred-memory cells into ports; rewrite them for the crossing walk (#326) | `Memory`, `MemPort`, `find_memories`, `crossing_view` |
| `abstract.py` | Detect + summarise single-clock subtrees to a port boundary (#256) | `is_single_clock_subtree`, `summarise_subtree` |
| `hierarchy.py` | Compositional boundary walk: summarise each shared module once (#257) | `compose_boundaries`, `CompositionStats` |
| `sdc.py` | Parse the CDC-relevant SDC subset | `ClockSpec`, `Clock` |
| `sdc_objects.py` | Resolve SDC object patterns against the netlist; object-scoped false-path coverage and acknowledgement (#328, §6.4) | `NameIndex`, `resolve`, `coverage`, `acknowledge` |
| `rules.py` | The CDC-001..-008 rule pack | `Violation`, `RULES`, `run_all` |
| `waivers.py` | Per-violation suppression with regex | `Waiver`, `SuppressedViolation` |
| `pragma.py` | Scan in-RTL `// rbcdc:` pragmas into `Waiver`s | `scan`, `scan_text` |
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
chains like parent `a_q` ← child `q` collapse to a single net. A
continuous `assign` additionally rewrites every cell emitted *earlier
in source order* that read the net's placeholder bits (applied once at
the end of the walk), so `if (pop && !empty)` above
`assign empty = …` reads the flag's real driver (#326).
Expression shapes lower to the Yosys cell zoo (binary →
`$and`/`$or`/`$xor`/…; unary → `$not`/`$logic_not`/`$neg`/
`$reduce_*`; conditional → `$mux`; element- and range-select →
bit subsets; concat and replication → pure LSB-first bit-tuple
aliasing, no cell emitted to match Yosys post-`opt_clean`).

Cell **pin order is part of that parity contract**, not just cell
type. Yosys' `$mux` is `Y = S ? B : A`: pin `A` is the `S=0` leg and
pin `B` the `S=1` leg, so `s ? a : b` lowers to `$mux(A=b, B=a, S=s)`
— the ternary's *false* branch on `A`. Source order is not pin order.
The rule pack reads mux legs **positionally** (§7's clock-mux clause
returns the first of `A`, `B` that resolves; CDC-012's gating
detection and `_is_gated_bus_crossing` inspect the data legs), so a
frontend that emits the legs the other way round inverts the select
polarity and silently changes findings rather than failing loudly.
Issue #289 was exactly that: the slang frontend's
`ConditionalExpression` lowering preserved source order, and a
`--frontend slang` run over a DFT clock mux resolved the flop to the
*other* clock leg and reported no crossing where the Yosys build
reported one. The three statement-level emitters (`always_comb`
`if`/`else`, `always_comb` `case`, and the `always_ff`
hold-feedback mux built by `_build_hold_mux`) already followed the
convention and were unaffected.

Every emitted cell carries an `attributes["src"]` string formatted
as Yosys' `"file:line.col-line.col"` convention, so the JSON /
SARIF reporters surface clickable source locations without a
frontend-specific branch.

Procedural blocks are lowered by what they infer, not by keyword
(#345): `always_ff` and an edge-triggered plain `always` take the flop
path; `always_comb`, `always @*` and a level-sensitive `always` that
assigns every target on every path take the combinational path;
`always_latch` and a latch-inferring level-sensitive `always` of the
`if (en) q = d;` shape become `$dlatch`. A procedural block the
builder cannot lower (mixed edge/level event list, delays, event
controls inside the body, an unrecognised clock expression) is
reported with a `WARNING` naming its source range instead of
vanishing silently; `initial` / `final` are not modelled.

Blackbox boundaries (#345): an instance of a module with a truthy
`(* blackbox *)` attribute, or one named by `lint --blackbox` /
`--greybox`, is not inlined. It becomes one cell named by its
flattened instance path, `type` = module name, `connections` keyed by
port name, and `parameters` holding only the parameters the
instantiation overrides, encoded as Yosys' `write_json` does — the
same cell a Yosys netlist carries, so CDC-022's XPM-default fallback
and `--sync-primitive` recognition behave identically.
`frontends.slang.elaborate_with_blackboxes` returns the module as an
`is_blackbox` sibling, the `(top, blackboxes)` pair
`netlist.load_with_blackboxes` returns (§4.8). The sibling's port
widths come from the first instance's parameters (Yosys sizes them
from the module defaults; the analysis reads widths off the
connections). A greybox sibling carries its lowered body.
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
`user_gray_flop_names` in `rules.py`). Two attribute families are
*port*-scoped instead — `reset_polarity` (§8, RDC-002) and the
scan-mode set (§8.20) — because what they declare is a property of the
external pin, not of a producing flop.

### 4.1a Unobservable cells (rtl-buddy-cdc#338)

Before any analysis, `cli._analyze_module_and_report` passes the top
module (and every greybox body) through `liveness.prune_dead_cells`.
A cell is **live** iff it is backward-reachable from an observation
point: an `output` / `inout` port bit, a netname bit with a truthy
`keep` attribute, or an input pin of a cell that is not a known pure
Yosys cell (blackbox / library / hierarchical instances, read through
the blackbox definition's port directions when the netlist has one and
as all-inputs otherwise; memories; `$assert` / `$print` …; any
unlisted type; any cell with `keep`). A live pure cell makes all its
input pins live, clock and enable pins included. Dead cells are
dropped from `Module.cells`; ports and netnames are untouched.

Consequences, deliberately:

- A flop whose `Q` reaches no observation point is dead with its whole
  input cone, so a crossing into it is **not reported**: nothing it
  captures can reach an output. It is also the flop `opt_clean`
  deletes, so the verdict on a netlist equals the verdict on that
  netlist after `opt_clean` (held for the whole corpus by
  `tests/test_gate_level_corpus.py::test_verdict_invariant_under_opt_clean`).
- Conservative: unknown cell types are never pruned, and a module with
  no observation point at all is analysed whole (a hand-made fragment,
  not a design that computes nothing).
- One linear pass per run; rules see only live cells and need no
  liveness checks of their own. The design summary's cell count is the
  live count.

### 4.1.1 Gate-level registers are regrouped at load (rtl-buddy-cdc#337)

After `techmap` / `abc` a W-bit register is W one-bit `$_DFF_*`
cells, and every rule that reasons about a bus (CDC-004 / 012 / 019 /
020, the gated- and gray-bus acceptances, CDC-014's width guard) keys
on one flop cell carrying the bus. `netlist.load_with_blackboxes` runs
`regroup.regroup_flops` on every module it parses, so the analyzer —
crossing enumeration, abstraction and the rule pack alike — sees the
shape `proc` would have produced:

- **Registers.** Gate flops are grouped by their `Q` bits' owner
  netname (public, ≥ 2 bits; the narrowest wins, so a concatenating
  wire never merges two registers; a port name is passed over when the
  bit has a name of its own; `splitnets`-style `q[3]` names are
  rebuilt into `q`) and split by control key: family, clock net and
  edge, reset / set / load / enable nets and polarities, and the
  cell's `src` (the `always` block). Each group of ≥ 2 lanes becomes
  one coarse cell (`$dff` / `$adff` / `$dffe` / `$adffe` / `$sdff*` /
  `$dffsr*` / `$aldff*`) named after the netname, lanes in netname
  order, per-bit reset values in `ARST_VALUE` / `SRST_VALUE`. The cell
  carries `REGROUPED_ATTR` listing the replaced cells.
- **Lane-wise logic.** From each rebuilt register's `D`, a vector whose
  bits are the outputs of distinct one-bit gates of one type
  (`$_MUX_` with one shared `S`, `$_AND_` / `$_OR_` / `$_XOR_` /
  `$_XNOR_` / `$_NOT_` / `$_BUF_`) is rebuilt as the equivalent wide
  `$mux` / `$and` / … / `$pos`, recursively. That restores the enable
  and sync-reset muxes and the gray encoder's `$xor` exactly.
- **Not regrouped.** Coarse cells are never touched (an RTL netlist is
  returned unchanged). A flop with no qualifying netname, or a bit of a
  register whose name names one flop twice (merged bits), stays one bit
  wide; if it also has no one-bit name of its own it is tagged
  `ORPHAN_ATTR`, and CDC-004 reports orphans of one `always` block that
  cross together as a possible bus (`warning`) rather than letting W
  clean one-bit chains go silent.

Where synthesis re-derived the logic (`abc`), two rule helpers fall back
to the gate-level form: `_is_gray_encoded_source` checks, by truth
table (`gate_eval.cone_tables`, ≤ 10 leaves), whether a rebuilt
register's `D` is the gray code of another register's `D` or `Q`; and
CDC-019 groups one-bit gate drivers by the multi-bit netname their
outputs share (§8.7). Both apply only to gate-level cells, so no
RTL-level verdict depends on them.

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

`FlopDomain` ties a `Flop` to a `clock: str | None` — the clock that
ultimately drives its CLK pin, or `None` if no root could be resolved
within the trace depth budget. The analyzer treats `None` as "domain
unknown" and excludes those flops from crossing detection rather than
emitting a false positive. A flop behind a port that carries several
clocks (`create_clock -add`) gets the port's primary clock, which
stands for all of them (§4.5, "Multi-clock ports", #330).

The clock is the trace's *raw root* — the top-level port, the
`create_generated_clock` pin target (§5.1), or an undeclared internal
net — put through `ClockSpec.clock_for_port`, so a port carries the
name of the clock declared on it (`create_clock -name core [get_ports
clk]` → `core`). **One flop has one domain on a run.** The crossing
walk, the rule context (`_RuleContext.domains`), CDC-023's combine
walk, the scan-mode walk, CDC-024's launch domains, memory port clocks,
the clock-network surface and the boundary clock-pin walk all take the
same four SDC inputs — `ClockSpec.clock_root_inputs()`: `pin_clocks`,
`clock_for_port`, `declared_clock_for_port` (§5) and `case_analysis`
(§5.2) — plus the run's `max_depth`. Before rtl-buddy-cdc#358 the rule
context was built without the first two, so a rule comparing
`ctx.domains[f]` with a crossing's `dst_clock` compared a port name with
a clock name, and every synchroniser on a renamed or pin-declared clock
read as depth 1. The rule context keeps the raw roots too
(`_RuleContext.clock_roots`, from the same trace) for the one rule that
needs a port name: CDC-021, naming an undeclared clock port that
`clock_for_port` would otherwise turn into the `<unconstrained>`
sentinel.

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
    scan_mode: bool = False      # #45: dst flop clocked through a DFT scan structure
    async_via: tuple[str, str] | None = None  # #330: co-located clock pair it is async through
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

`async_via` (#330) is stamped by `filter_async` on a crossing whose
endpoint sits behind a multi-clock port and which is asynchronous only
through a clock *co-located* with the port's primary (§4.5,
"Multi-clock ports"). `src_clock` / `dst_clock` keep the primary names
— the names the flops have, which every rule compares domains against —
and `async_via` names the pair that is actually asynchronous. It is
`None` on every crossing of a design without a multi-clock port.

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
    port_delays: list[PortDelay]                   # those statements, targets as written (#357)
    pin_clocks: dict[str, str]                     # internal-pin target → generated clock name
    datapath_only_delays: list[DatapathOnlyDelay]  # every set_max_delay -datapath_only (#325)
    datapath_only_pairs: set[frozenset[str]]       # …the clock-to-clock ones, as pairs
    false_path_exceptions: list[FalsePathException]  # object-scoped / -through set_false_path (#328)
    case_analysis: dict[str, str]                  # set_case_analysis port → "0"/"1"/"rising"/"falling" (#325)
    multi_clock_ports: dict[str, tuple[str, ...]]  # port → its clocks, primary first, ports with ≥2 (#330)
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
  async to `clk` despite sharing a master. A clock pair in
  `datapath_only_pairs` (by name, or by resolved roots) is async
  too, and is checked in step 1 — see "Datapath-only max delay"
  below.
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

**Multi-clock ports (issue #330).** SDC lets a source object carry
several clocks: `create_clock -name clk_a2 -add [get_ports clk_a]` on a
port that already has `clk_a` keeps both (typically two modes on one
pin, declared `-physically_exclusive` / `-logically_exclusive`). Without
`-add`, a later `create_clock` on the same port *replaces* the earlier
one — the parser strips the port from the earlier clock, removes that
clock once it has no port left, and warns (`_claim_ports`). So after a
parse a port carries several clocks only through `-add`, and
`parse()` records them in `ClockSpec.multi_clock_ports` (port → clocks
in declaration order).

The data model keeps one clock per flop (`FlopDomain.clock: str |
None`). Rather than a composite domain, the port's **primary** clock —
the first declared, which is what `clock_for_port` returns — names every
flop behind it, and that name **stands for every clock on the port**:

- `colocated_clocks(name)` is `(name,)` unless `name` is a primary,
  then `name` followed by the port's other clocks in declaration
  order. A non-primary clock (`set_input_delay -clock clk_a2`) means
  only itself; a port typed with the primary means the whole port.
- `are_async(a, b)` is true iff **some** pair in
  `colocated_clocks(a) × colocated_clocks(b)` is async (per-pair rules
  above) **and** not unreachable — a crossing is synchronous only if it
  is synchronous to every clock on the port. `is_unreachable_crossing`
  is true only if **every** pair is unreachable, so the usual exclusive
  group between the two modes removes just that pair.
- `async_witness(a, b)` returns the first such pair, scanning `a`'s
  clocks then `b`'s in declaration order — deterministic, and identical
  on both SDC backends (they hand the handlers the same commands in the
  same order). `filter_async` keeps a crossing iff it has a witness and
  stamps `Crossing.async_via` when the witness is not `(a, b)` itself;
  `rules.run_all` appends `[multi-clock port] asynchronous as X → Y` to
  every finding on such a crossing, and the JSON report and domain map
  carry `async_via`.

For two single-clock names every predicate is exactly the per-pair
answer, so a design without a multi-clock port is unaffected. Why this
shape: a composite domain (`"clk_a|clk_a2"`) would have to be taught to
every consumer that compares domain names or looks a clock up by name
(sync-chain walks, period lookups, the JSON schema), while every async
question in the analyzer already goes through `are_async` — making that
one predicate port-aware gives all of them (rules, `clock_network`,
`abstract`, the compositional pass, which copies `multi_clock_ports`
into its sub-spec) the conservative answer at once. The costs: the
primary's name in a message can be a clock the crossing is *not*
asynchronous to (hence the suffix), a clock with several ports expands
for all its flops if it is primary on any of them, and period-based
rules (CDC-013 / CDC-014) still read only the primary's period.

`create_generated_clock` follows the same replace/add rule on port
targets. Internal-pin targets (`pin_clocks`) still hold one clock per
pin: a replacement warns, and `-add` on a pin is not modelled (the first
clock stays; warning). A generated clock's master comes from
`-master_clock` only — `-source` (recorded on `Clock.source`) never
infers one — so a `-master_clock` naming one co-located clock
disambiguates exactly, and a multi-clock `-source` with no
`-master_clock` (or one naming none of its clocks) is a G-11 warning
(§8.14) rather than a silent pick.

**Datapath-only max delay (issue #325).** `set_max_delay <v>
-datapath_only -from [get_clocks A] -to [get_clocks B]` bounds the
skew of an intentional asynchronous crossing (a gray bus, a handshake
payload) without declaring a false path. The bound only exists where
the launch and capture clocks are *not* timed against each other —
`-datapath_only` drops the clock relationship from the check — so a
clock-to-clock datapath-only constraint is read as **the pair is an
intended asynchronous crossing**: `are_async` returns True for it,
exactly as for a `set_false_path -from/-to` clock pair.

Where it contradicts a declaration that says *synchronous* — the two
clocks are generated from one master, or share a `-group` of a
`set_clock_groups -asynchronous` (members of one group are timed
together) — the datapath-only bound **wins**. That is the conservative
choice for a CDC linter: it keeps the crossing in the async list, so
its synchroniser is checked, rather than trusting the relationship
the author just told STA to ignore. A `set_false_path` on a same-root
pair still loses to the collapse, unchanged. The same reasoning makes
a datapath-only pair never *unreachable*: `is_unreachable_crossing`
returns False for it even when the clocks sit in different exclusive
groups, because bounding a path's delay says its author believes the
path is live. A pair declared between two masters also covers clocks
generated from them (the check runs on the given names and on the
resolved roots). Adding a datapath-only pair can only move a crossing
*into* the async list, never out of it, so it cannot reduce findings.

Each constraint is recorded as a frozen `sdc.DatapathOnlyDelay`
(`value`, `from_clocks`, `to_clocks`, `from_objects`, `to_objects`).
Endpoints other than clocks — `[get_cells …]`, `[get_pins …]`,
`[get_ports …]`, `[get_nets …]` — are kept as `(kind, name)` pairs in
SDC `/` notation and **not resolved** against the netlist; such a
record never contributes a clock pair and raises a partial warning.
Resolving object endpoints to flops is the seam left for issue #328
(object-scoped `set_false_path`), which will need the same
cell/pin → flop mapping for both commands. `-through` narrows the
constraint to specific paths and is not recorded (warning), mirroring
`set_false_path -through`. Plain `set_max_delay` without
`-datapath_only` is gated off before dispatch (`sdc._COMMAND_GATES`)
and dropped exactly like an unrecognised command.

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

#### Inferred-clock candidates (issue #263, P3)

A frequent cause of under-resolution is an **undeclared internal
generated clock** — a divided / forwarded clock the user forgot to
declare with `create_generated_clock`. `domain.find_inferred_clock_candidates`
reports each net bit that

1. drives at least a threshold (default 4) flop `CLK` pins,
2. is produced by a flop `Q` or a clock-gate / ICG / latch output (the
   same clock-network cell families `trace_clock_root` walks), and
3. is **not** already a declared clock. Three declaration forms count:
   a top-level input port, a `create_generated_clock` target
   (`pin_clocks` → `_build_bit_to_clock`), and a plain `create_clock`
   aimed at an internal pin (`[get_pins <net>]`), whose target lands in
   `Clock.ports` and is read back through `ClockSpec.clock_for_port` —
   the same lookup `assign_domains`' `clock_identity` predicate uses.
   Consulting only the first two re-reported a clock the user had
   already declared (#270); `domain._declared_clock_bits` closes that.

The report is surfaced as:

- JSON `inferred_clock_candidates` (list of `{driver, driver_kind,
  fanout, example_sinks}`) — `driver_kind` is `"flop"` or `"gate"`,
  `example_sinks` a bounded sample of sink-flop cell names.
- Text report — a cyan `ⓘ` advisory line per candidate naming the
  driver and its CLK-pin fanout.

This is **advisory only** and the highest silent-false-negative risk in
the whole epic, so it defaults to *report-only*: it is computed from the
netlist + SDC pin map, never feeds back into `assign_domains` /
`find_crossings`, and so **cannot change a flop's domain, a crossing, or
a violation**. A flop behind an undeclared internal clock stays
`domain_unknown` (and is still counted there) unless a real clock-root
trace already resolves it — the divider / latch clauses of
`trace_clock_root`, which fire independently of this fanout heuristic.
Auto-assigning a clock identity from the heuristic alone is forbidden:
it could make two async groups read same-domain and silently drop a
crossing. The contract is "report; let the human declare it". The key is
deliberately **not** in `JSON_CONTRACT` — it is an additive advisory,
not a pinned downstream count.

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
    src_clock: str | None # output: domain driving the port; input: capture domain
    synchronised: bool    # True iff the port is PROVEN to pass a synchroniser
    width: int            # number of bits on the port
    sync_depth: int | None = None      # #261: the proven chain's depth
    user_synchronised: bool = False    # #261: proven via a USER_SYNC_ATTRS tag

@dataclass(frozen=True)
class BoundarySummary:
    module: str                          # the summarised module's real (non-$) name
    ports: dict[str, PortBoundary]       # output/inout ports (virtual sources)
    clock: str | None = None             # P3 #257: the subtree's own resolved clock
    input_ports: dict[str, PortBoundary] = {}  # P3 #257: input/inout ports (virtual sinks)
    internal_analysed: bool = False      # #261: the module's own body was analysed
    reconvergent_inputs: frozenset[frozenset[str]] = frozenset()  # #261
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

**Greybox: a boundary module that kept its cells (#261).** The blackbox
above has *zero* cells by construction — `read_slang --blackboxed-module`
discards the body — which is why §4.9's `synchronised` field had no way to
be proven and stayed inert. A **greybox** is the same object with its
internals intact: a module carrying `attributes.blackbox` (so `flatten`
leaves it standing and the parent keeps an ordinary instance cell) **and**
its cells. Yosys produces one with
`setattr -mod -set blackbox 1 <module>` between `proc` and `flatten`;
`lint --greybox MODULE` is the CLI surface (a plain Yosys pass — no plugin
needed, unlike `--blackbox`; the slang frontend builds the same sibling
natively, #345). Nothing in the loader changes: `is_blackbox`
still comes from the attribute, and a zero-cell sibling still takes the
pre-#261 path exactly as before. The whole of §4.9's compositional
machinery is gated on `sub.cells` being non-empty.

See §4.9 for what this collapse preserves versus discards, and for how
the `synchronised` field is now proven.

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

**Four soundness guards close the silent false-negatives the #259 audit
found, plus the clock-output hole #273 found.** All of them err toward
*declining* abstraction — the conservative direction for a CDC checker.

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
  is what is excluded from input-sink seeding.

  *Retired for a greybox (#261):* with the module's internals in hand a
  dual-clock IP is no longer declined — it is analysed on its own body and
  summarised **per port**, each stamped with the domain that really
  captures or launches it. The decline stands for a stub blackbox, and for
  the three residual cases listed at the end of this section.
  `compose_boundaries` keys its summary cache on the **clock-pin → root
  mapping** (a strict refinement of the earlier root frozenset: two
  instances of one dual-clock module wired src/dest in opposite directions
  share a root set but need opposite per-port domains — the same collision
  §4.10 records for the sync-primitive path), so a dual-clock instance
  keys distinctly while identical instances still hit the cache.

- **Clock-output decline (`abstract.clock_driving_output_ports`, issue
  #273).** Being provably single-clock is not enough: a candidate whose
  **output port drives a clock** *generates or forwards* a clock consumed
  elsewhere, and blackboxing it elides that clock generation/forwarding
  network. The forwarded clock then leaves an opaque boundary output —
  downstream consumers go domain-unknown, or vanish entirely when they
  are abstracted too — and the still-visible muxes / ICGs / buffers
  feeding the boundary lose their clock-distribution status, so CDC-008
  ("clock used as data") false-fires on them (its `_clock_network_cells`
  exemption is seeded only from *surviving* flop `CLK` pins). Such a
  candidate is declined.

  An output bit "drives a clock" when it reaches, directly or through a
  bounded forward walk over ordinary combinational cells, one of three
  sink kinds: a flop `CLK` / `C` pin, an **SDC-declared clock net**
  (a `create_clock` port — input or output, forwarding off-chip counts —
  or a `create_generated_clock` internal-pin target), or a **clock input
  pin of another blackbox / boundary instance**. That third kind is taken
  from `blackbox_clock_pins_by_module` — the *union* of each module
  type's per-instance traced clock pins — because on a forwarding mesh
  the downstream tile's `clk_in` is driven by an opaque boundary output
  and so does not trace to a declared clock on its own; the upstream
  instance of the same type is what proves the pin is a clock pin of the
  module. No pin-name guessing enters here (`_looks_like_clock_name`
  would misread `dest_ack` — it contains the `ck` hint). The walk stops
  at flops (a `D` pin is a data sink) and at other blackbox boundaries
  (opaque, and their clock pins are already a sink kind); its hop budget
  is the same `--clock-trace-depth` value the tracer uses in the opposite
  direction.

  The verdict is **per module type**: any qualifying instance declines
  the type, so a tile whose top-level instance leaves `clk_out`
  unconnected is declined too (it is the same module, and abstracting it
  there would be just as unsound). It is decided in a pre-pass in
  `compose_boundaries`, ahead of the §4.10 primitive path — a sanctioned
  synchroniser that also forwards a clock would elide the clock network
  in exactly the same way, and the `--sync-primitive` promise is about
  the *data* crossing. The offending `(module type, output port)` pairs
  are recorded in `CompositionStats.clock_output_ports` so the CLI can
  name the port. A module whose outputs carry only data — the
  overwhelmingly common case — is never declined by this guard.

  The practical effect is that blackbox granularity becomes
  self-guiding: on a hierarchical clock-forwarding mesh, an attempt to
  blackbox the *tile* is declined and the message points one level
  deeper, at the clock-output-free datapath core.

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
  acknowledged one. Both decline flavours share this one path and one
  rule id, so waiving works identically; only the message differs —
  `… left opaque — not provably single-clock; …` versus `… drives a clock
  output \`clk_out\` — clock generation/forwarding would be elided; flatten
  it or analyse standalone …`. (`CDC-BBX` is emitted by the CLI orchestration, not the
  rule pack — it is an analysis-coverage finding, not a structural rule.)

- **Reconvergence-unsafe skip (`hierarchy.reconvergence_unsafe_instances`,
  FIX 3).** A single-clock block that *is* abstracted but has ≥2 distinct
  foreign-domain crossings entering **distinct** input ports can hide an
  internal reconvergence (CDC-005) the flat design would flag — the
  star-collapse severs the subtree-internal graph, so that reconvergence
  cannot be checked at the boundary. **Retired per instance for a greybox
  (#261):** `reconvergence_unsafe_instances` takes the boundary map and
  exempts any instance whose summary carries `internal_analysed`, because
  the severed fact is back (`reconvergent_inputs`) and CDC-005 re-raises
  it at the boundary. The gate existed because the collapse destroyed
  information; once the information is restored, declining would only
  destroy coverage. After `find_crossings`, boundary-sink
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
now live, via route (a) (#261).** It was hard-wired `False` by the
summariser and consumed nowhere, so the model assumed *no* boundary path
was synchronised — the direct source of the conservative over-reporting
above, and of the #259 declines below. Two things could have supplied the
bit: **(a)** a real per-module analysis pass, or **(b)** a user
annotation. `compositional.py` implements (a), and honours (b) along the
way (a `USER_SYNC_ATTRS` tag on the proven first/last stage counts).

- **Soundness asymmetry (unchanged, and the reason everything below is
  shaped the way it is).** Every other approximation in this model errs
  toward over-reporting. `synchronised=True` is the *only* lever that can
  make the tool **under**-report — trust a synchroniser that is not there
  — so it may be set only when **proven** or **explicitly promised**, and
  it defaults `False`.

**The compositional pass (`compositional.analyse_module`).** Given a
greybox module (§4.8) and the instance's clock-**pin → root** mapping —
the clock context `compose_boundaries` already computes — it runs the
ordinary pipeline (`assign_domains` → `find_crossings` → `run_all`) over
the module **in isolation**, once per `(module type, clock context)`. The
parent contributes nothing but that mapping, which is exactly what makes
the result cacheable and the block analysed once however many times it is
instantiated.

The SDC is *projected* onto the subtree's own namespace by
`derive_sub_spec`: each parent clock **root** is re-declared as a clock
whose port list is the subtree pin carrying it, and the async / exclusive
/ false-path partitions are copied verbatim, so `are_async` gives
identical answers on both sides of the boundary. The parent's own port
lists are **stripped** (a subtree data port that happens to share a name
with a parent clock port is never mistaken for a clock), and two things
deliberately do not travel: `port_clock` typings (a subtree input is a
hierarchy pin, and the parent's boundary-sink crossing is what reports
data entering it) and `pin_clocks` generated-clock targets (their paths
are parent-relative).

Four rules are excluded from the internal pass because their predicate
is literally *"a top-level port of the design"*: **CDC-006** (a
synchroniser fed from an unregistered top port with no source flop — the
definition of a correct boundary input synchroniser), **CDC-011**
(untyped primary input — no subtree pin is ever typed), **RDC-008**
(async reset driven directly by a top-level port — what every reset pin
is), and **CDC-025** (uncontracted top-level output, §8.22 — a subtree
output pin is an internal net of the parent). The input-side three are
still reported, by the parent, as the boundary-sink crossing at that
same port; whatever captures a subtree output inside the parent is a
crossing the parent's own walk reports.

**What the pass publishes, and what each fact does.**

- **Per-port `sync_depth` / `synchronised` (inputs).** For a *width-1*
  input port, the proof requires: the port bit has **exactly one** reader,
  that reader is a flop's `D` pin, the flop is 1-bit and lands in a known
  domain, and the port bit is not also an output-port bit. Then
  `_sync_chain_depth` measures the chain from that flop. A tie-off (no
  reader), a comb bypass or any second load (≥2 readers), a landing on an
  enable/reset pin, a multi-bit port, or a feed-through all **defeat** the
  proof and fall back to the conservative default. A multi-bit port is
  never proven on purpose: a bus through per-bit 2FF chains is a
  data-coherency bug in its own right, and claiming the boundary handled
  it would suppress the CDC-004 crossing the bus rules must see.

  **Effect of `True`: the port is NOT dropped.** §4.9 originally proposed
  omitting a synchronised port from `ports` / `input_ports` so it raised
  no crossing. The implementation takes the softer, *parity-preserving*
  variant instead: the virtual sink stays, so the crossing is still
  reported, and the **proven depth** is published to the rule pack
  (`_RuleContext.boundary_sync_depth`, consumed through
  `_effective_sync_chain_depth`). A virtual sink has an empty `Q`, so the
  structural walk can only ever answer depth 1 — which is why an
  abstracted single-bit input used to fire CDC-001 unconditionally. With
  the real depth in hand, CDC-001 / CDC-002 / CDC-003 / CDC-005 behave at
  the boundary exactly as they do on the flattened design: quiet at depth
  ≥ 2, and CDC-002 still fires when the proven depth is under a raised
  `--sync-depth`. Dropping the port would have lost the crossing from the
  report and the depth with it; keeping it is both more conservative and
  more precise. A user-tagged first stage is published separately and
  merged into `ctx.user_syncs`, so route (b) needs no rule changes at all.

  **Chain truncation is safe by direction.** The boundary can only count
  the stages *inside* the block; a parent-side flop that extends the chain
  is invisible to it. That under-counts depth, which over-reports — the
  correct direction.

- **Per-port `synchronised` (outputs).** §4.9's documented meaning —
  every register→port path passes a synchroniser — proven when each port
  bit is driven *directly* by a flop `Q` (a comb cell between the last
  register and the port defeats it) and that flop is the tail of a
  ≥2-stage same-domain chain whose inter-stage net has exactly one
  reader, or is `USER_SYNC_ATTRS`-tagged. Output-side `synchronised` does
  **not** suppress a crossing: as §4.10 already establishes for
  recognised macros, stamping the port with the domain that *really*
  drives it is what makes the suppression correct, and a sink in a third
  domain must still be reported.

- **Internal reconvergence (`reconvergent_inputs`).** Walking forward
  from each input port's capture-chain terminal and intersecting the
  downstream cones gives the input-port groups whose captured values
  recombine inside the block. CDC-005 re-raises at the boundary
  (`rules._boundary_reconvergence`) when two crossings **from one source
  flop** enter two ports of a recorded group — the same predicate the flat
  rule applies, evaluated on the fact the star-collapse severed.

- **Internal findings, lifted.** Everything the rule pack finds inside
  the module is reported in the parent's output, **once per module type**,
  with the instances it covers named in the message
  (`hierarchy.LiftedAnalysis`). `cell_name` is re-anchored on the first
  instance, so the finding resolves to a source location in the parent and
  is waivable by boundary instance (`waive CDC-002 u_afifo`); the internal
  cell is not addressable from up here. Reporting once — not once per
  instance — *is* the analyse-once contract made visible.

- **Per-port domains for a multi-clock module.** Each port is stamped
  with the domain that genuinely captures (input) or launches (output) it,
  so the star-collapse becomes **per-domain** instead of one hub. This is
  what lets a dual-clock IP be summarised at all rather than declined:
  `_boundary_sink_flops` seeds each virtual sink at `PortBoundary
  .src_clock`. A single-clock module deliberately keeps the whole-block
  domain on every port, so its result is bit-identical to the pre-#261
  one apart from the added facts.

**What still declines, and why.** A conservative decline carrying a
`CDC-BBX` diagnostic is acceptable; silence is not. Three cases survive:

1. **An internal flop that lands on no parent clock root.** This is the
   case pin inspection *cannot* see, and the pass's most valuable catch.
   A flop clocked from a module port no classifier accepts (`strobe`,
   `go`) resolves inside the module to that port's own name, which is a
   declared clock nowhere — so `are_async` answers False for every pair
   involving it and its crossings are filtered away. Pre-#261 such a
   module read as *single-clock* and was abstracted, dropping its internal
   crossing with **no diagnostic at all**. It is now declined loudly.
2. **A multi-clock module with an ambiguous input.** An input captured in
   two internal domains cannot be represented by one virtual sink, and
   picking either would drop the other's crossing.
3. **A multi-clock module with no internal registers at all** — nothing
   to attribute any port to.

An input nothing sequential captures (a pure combinational gate input) is
not a decline: it simply seeds **no sink**, because there is no crossing
*into* the block there. What it gates leaves through an output, which is
seeded as its own source — unconstrained when that output is a comb
feed-through, so the path stays covered conservatively.

**Residual imprecision, stated plainly.** A rule whose subject *spans*
the boundary is evaluated on each side separately, because that is what a
hierarchy boundary means: CDC-018's chain-length smell, for instance, sees
the block's internal chain and the parent's chain but never their
concatenation. This affects smell-level rules about path length, never
the presence of a crossing.

### 4.10 Recognised CDC primitives (issue #275)

The star-collapse of §4.9 assumes the boundary is a *design* subtree the
analyzer would otherwise have to walk. A **vendor CDC macro** is a
different kind of object: it is dual-clock by construction (`src_clk` +
`dest_clk`), so `is_single_clock_subtree` is False and
`summarise_subtree` declines it — producing a `CDC-BBX` coverage error
per instance *and* dropping the crossing through it entirely (a declined
instance is absent from `boundaries`, so neither a boundary source nor a
boundary sink is seeded). On an XPM-based design that is both a false
positive flood and a silent blind spot.

`primitives.py` is the registry that resolves this: a small table of
module names (the seven-member Xilinx `xpm_cdc_*` family, plus any
`--sync-primitive` registrations) that the analyzer treats as
*sanctioned synchronisers*. `compose_boundaries` consults it **before**
the generic path and, on a hit, calls
`abstract.summarise_sync_primitive` instead of `summarise_subtree`.

**Recognition is by module NAME, not by elaborating the macro.** XPM
sources live in the vendor install tree and are injected at synthesis, so
a filelist built from project RTL does not contain them — an
elaboration-based recogniser would serve almost nobody. The name is
sufficient because the library's port convention is rigid: every
data/control port is `src_*` or `dest_*` and every clock pin is
`src_clk` / `dest_clk`.

The resulting `BoundarySummary` differs from a §4.9 summary in three
ways, and each is load-bearing:

1. **`clock` is the destination clock**, resolved from the `dest_clk`
   pin specifically rather than from "the sole root". The clock pins
   surfaced by `_instance_clocks` are classified by name
   (`clock_pin_role`); a macro with exactly one, unclassifiable, clock
   pin takes it as the destination. If the destination cannot be
   identified — two clock pins that say neither `src` nor `dest`, or an
   unresolved `dest_clk` — the summariser returns `None` and the
   instance **falls through to the generic path**, i.e. is declined and
   reported. Registering a macro is not a licence to skip it.
2. **Each output port is stamped with the domain that actually drives
   it** and carries `synchronised=True` — the first real use of the
   `PortBoundary.synchronised` hook §4.9 reserved. `dest_*` outputs are
   launched from the destination clock, `xpm_cdc_handshake.src_rcv` from
   the source clock. Port *width* comes from the parent-side connection,
   not from `sub.ports`: a `dynports` blackbox stub reports its
   default-parameter width, not the instantiated one.
3. **`input_ports` is empty** — no virtual sink. A crossing into a
   recognised synchroniser's data input is safe by construction, which
   is the whole reason the macro exists. As a side effect the
   reconvergence gate (`reconvergence_unsafe_instances`) can never trip
   on a macro with two data inputs, since it groups on `dst_boundary`.

The suppression that results is a *consequence of naming the right
domain*, not an unconditional skip. Because each output carries its true
domain, a `dest_out` consumed by a flop in a **third** domain is still
emitted by the ordinary `dst_clock != src_clock` test in
`find_crossings`. The macro's own crossing is accepted; a crossing the
macro does not cover is kept.

Instances are deliberately **not cached** by `compose_boundaries`. The
cache key is `(module type, frozenset of clock roots)`, and a frozenset
cannot distinguish two instances of the same macro wired src/dest in
opposite directions — they would collide and one would get the other's
destination clock. Tracing two clock pins is cheap; correctness is not
negotiable here. `CompositionStats.primitive_modules` records the
recognised types (a subset of `boundary_modules`).

A complementary, zero-XPM-code route serves users who *do* have the
sources: XPM tags its internal stages `(* ASYNC_REG = "TRUE" *)`, and
`user_sync_flop_names` matches `USER_SYNC_ATTRS` case-insensitively, so
flattened internals are recognised through the ordinary `(* cdc_sync *)`
machinery.

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
| Two-input gate (ICG) | `$and`, `$or`, `$logic_and`, `$logic_or`, `$xor`, `$xnor` | Trace both inputs; the side that resolves to a clock is the root, the other is the gate's enable. Two **distinct declared clocks** combining here decline and are reported as CDC-023 (see below) |
| Mux | `$mux`, `$pmux` | Trace each candidate input; first one to resolve wins (the analyzer can't statically know which side `S` selects). A mux *selects* — it does not combine — so it is not subject to the combine-decline, and never produces a CDC-023 finding |
| Case-analysed mux | `$mux`, `$pmux`, `$_MUX_` whose every `S` bit is a `set_case_analysis` constant | Follow **only** the selected leg (§5.2) — the mode fixes `S`. Replaces the first-resolves-wins row above for that cell |

| Two-input gate (ICG) | `$and`, `$or`, `$logic_and`, `$logic_or`, `$xor`, `$xnor`, and the gate-level `cells.GATE_LOGIC_TYPES` (`$_AND_`, `$_OR_`, `$_NAND_`, `$_NOR_`, `$_XOR_`, `$_XNOR_`, `$_ANDNOT_`, `$_ORNOT_`, `$_AOI3_`, `$_OAI3_`, `$_AOI4_`, `$_OAI4_`; rtl-buddy-cdc#336) | Trace every input (`A`/`B`, plus `C`/`D` on the AOI/OAI cells). The leg choice depends on the legs' clocks, not their order: every walk passes `declared_clock_for_port` (`ClockSpec.declared_clock_for_port`, `create_clock` ports only — a `set_input_delay`-typed data port is not a clock; rtl-buddy-cdc#358), so `en & clk` and `clk & en` resolve alike, and an enable typed to a foreign clock is not a second clock; the side that resolves to a clock is the root, the other is the gate's enable. Two **distinct declared clocks** combining here decline and are reported as CDC-023 (see below) |
| Mux | `cells.MUX_PINS`: `$mux`, `$pmux`, and gate-level `$_MUX_`, `$_NMUX_`, `$_MUX4_`, `$_MUX8_`, `$_MUX16_` (rtl-buddy-cdc#336) | Trace each candidate data input (`A`/`B`, or `A`…`P` on the wide gate muxes); every select pin (`S`, plus `T`/`U`/`V`) is reported as the mux's control; first one to resolve wins (the analyzer can't statically know which side `S` selects). A mux *selects* — it does not combine — so it is not subject to the combine-decline, and never produces a CDC-023 finding |
| FF (clock divider) | any of `FF_CELL_TYPES` reaching from a `Q` output | Recurse on the source flop's CLK pin; the divided clock inherits its domain identity from the upstream root |
| Transparent latch (clock-path ICG) | `$dlatch`, any `$_DLATCH_*` reaching from a `Q` output | Explore the latch's data pin (`D`) and enable pin (`EN` coarse / `E` gate-level); the leg that resolves to a declared clock is the root (whichever pin it enters on). A clock routed through a latch-based ICG can enter on either pin depending on the coding style. Two distinct declared clocks combining decline and are reported as CDC-023 (see below). **Clock-resolution only** — latch transparency here never reaches data-path crossing detection, which keeps treating a latch as an opaque endpoint (CDC-017). See issue #263 (P2) |

The walker maintains a per-call `seen` set keyed on bit ID and a
`max_depth` counter (default 16), so cycles in the clock network
terminate cleanly. The depth budget is intentionally low — clock
networks rarely exceed a handful of hops, and a deep walk is more
likely to be following data than clock.

**Clock-combining decline on multi-input clock cells (issue #263).**
The two-input gate clause and the clock-path latch clause both explore
several legs (gate: `A`/`B`; latch: `D`/`EN`/`E`) and resolve them
through one rule (`_pick_combining_root`), using a `clock_identity`
predicate built in `assign_domains` from the SDC (`clock_for_port` plus
the `create_generated_clock` set):

- **exactly one declared clock among the legs** → that clock, regardless
  of leg order. This is the common, safe ICG: one leg is a clock, the
  other a *non-clock* enable (e.g. a plain `clk_en` input port, which is
  not a declared clock and so is not a competing identity). The
  `icg_port_enable` fixture pins this.
- **two or more distinct declared clocks combining on one net** (e.g. an
  `$and` with `A=clkA, B=clkB`, or a `$dlatch` with `D=clkA, EN=clkB`)
  → **decline** (`None`). Such a cell mixes clock domains; the
  downstream flop's domain is genuinely ambiguous (its clock physically
  toggles on both), so the tracer refuses to assert one leg and the flop
  is surfaced as `domain_unknown` rather than silently mislabelled. The
  `clock_combine_latch` / `clock_combine_gate` fixtures pin this.
- **no declared clock among the legs** (an undeclared internal root, or
  a caller with no SDC clock context) → first-resolved leg, the prior
  behaviour, unchanged.

A **mux** is deliberately *not* subject to this — it selects one of its
clock inputs rather than combining them, so first-resolves-wins is
correct there. Two legs that map to the *same* declared clock (the
`ck0_a` / `ck0_b` ports of one `create_clock`, issue #166) are one
identity, not a combine, and resolve normally. The decline only ever
turns a previously *mislabelled* flop into a loud `domain_unknown`; it
never drops a crossing carried by the design's other, unambiguous flops.

**The decline is now also a finding (CDC-023, issue #269).** Declining
removed the mislabel hazard but said nothing about *why* the flop was
unresolved. `_pick_combining_root` therefore takes an optional
`on_decline` callback, invoked at the single line that returns `None`,
and `trace_clock_root` threads a matching `on_combine` recorder
(`domain.CombineSink`) down the walk. `domain.find_clock_combines` runs
the ordinary per-flop clock-root walk with that recorder attached and
returns one `ClockCombine` per distinct combining node — net, cell,
cell type, the declared clocks that met, and the flops reached. CDC-023
(§8.19) renders those records. Because the report is *emitted by the
decline site itself*, "the rule fired" and "the tracer declined" are
one event, not two predicates that have to be kept in agreement.

The declared-clock predicate lives in `domain._clock_identity_fn`,
hoisted out of `assign_domains` for the same reason: one function
serves both the tracer and the reporter, so the `<unconstrained>`
sentinel exclusion (and every future refinement) applies to both by
construction.

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

### 5.2 Case-analysed clock muxes

`set_case_analysis <0|1> [get_ports sel]` (issue #325) pins a mode port
to a constant. Without it, a clock mux presents both clocks and the
walker takes the first leg that resolves, so a flop behind the mux can
be filed under a clock the constrained mode never selects — and a real
crossing into the clock it *does* select goes unreported.

`domain._build_case_bits(module, ClockSpec.case_analysis)` expands the
port targets to constant net bits (`"0"` / `"1"` only — `"rising"` /
`"falling"` toggle and are left out; a bus bit `sel[3]` is bit 3 from
the LSB, since the netlist carries no declared range). The map rides
into `trace_clock_root` as `case_bits`, and the mux clause becomes
exact for any `$mux`, `$pmux` or `$_MUX_` whose **every** select bit is
a case constant:

- the select is evaluated by `_case_constant`, which follows the bit
  back through exactly the `_BUFFER_TYPES` the walker already treats as
  transparent, counting inversions (`$not`, `$logic_not`, `$_NOT_`);
  `$logic_not` / `$reduce_bool` count only on a one-bit operand. Any
  other driver stops it — this is select resolution, not constant
  propagation;
- `$mux` / `$_MUX_` pass `B` when `S=1` and `A` when `S=0`; a `$pmux`
  passes the `B` slice whose one-hot `S` bit is set, or `A` when none
  is, and is left unresolved when two are hot. The leg is lane-exact
  (`Y[k]` ← `A[k]` / `B[i*WIDTH+k]`);
- **only the selected leg is followed**, and followed exactly: if it
  does not resolve (a constant, an undeclared net), neither does the
  mux. There is no fallback to the other leg — in that mode the other
  leg's clock never reaches the flop.

`$_MUX_` is resolved here although the un-analysed walk does not follow
it (it is not in `_MUX_TYPES`): with a constant select the answer is
exact, and the change is invisible to any design without case analysis.

Every clock-root walk on a run is handed the same map — `assign_domains`
(and through it `find_crossings`), `find_clock_combines` (CDC-023),
`find_scan_mode_flops`, the rule context (`_build_context`), the
clock-network surface (`find_clock_network_crossings`) and the
boundary clock-pin classification (`abstract._instance_clocks`,
`summarise_sync_primitive`) — for the same reason `max_depth` is
threaded everywhere: two views of one flop's domain must not disagree.
Interactions, each pinned by `tests/test_sdc_case_analysis.py`:

- **CDC-023** — the combine walk follows only the selected leg, so a
  combine on the unselected leg is unreachable in that mode and not
  reported; a combine on the selected leg is reported and leaves the
  flop `domain_unknown` (it no longer falls through to the other leg).
- **CDC-010** — unchanged, and it never fired on these selects: a
  case-analysed select comes from a top-level port, and CDC-010 only
  fires when a control pin's backward fan-in reaches a *flop*. The
  case-resolved mux is still a clock-network cell for CDC-008 / CDC-010.
- **Scan-mode tagging (#45)** — a case-resolved select is not reported
  to `on_clock_control`: a constant select steers nothing in that mode,
  so the flop is not tagged as clocked through a scan structure
  (`set_case_analysis 0 scan_en` analyses functional mode, and its
  crossings are reported).
- **`find_inferred_clock_candidates`** — unaffected; it counts CLK
  fan-out by driver and never traces through a mux.

`domain.case_analysis_warnings` reports, through the end-of-parse
`partial_warnings` channel, every target that names no input port, an
out-of-range bit, or a non-input port — the same `_case_target_bits`
resolution the tracer uses, so "warned" and "ignored" are one decision.
Pin / cell / net targets are reported at parse time and ignored (a
case-analysed internal pin could be flop-driven, which CDC-010 would
then need to honour; out of scope). Data-path use of the constant is
out of scope too.

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
create_clock -name <name> -period <p> [-add] [get_ports <port> ...]
create_generated_clock -name <n> -master_clock <m> \
    -source <pin-or-port> -divide_by N [-add] [get_pins <pin>]
set_clock_groups -asynchronous          -group {…} -group {…} …
set_clock_groups -logically_exclusive   -group {…} -group {…} …
set_clock_groups -physically_exclusive  -group {…} -group {…} …
set_false_path  -from [get_clocks A] -to [get_clocks B]
set_false_path  -from <clocks|objects> -through <objects> -to <clocks|objects>
set_input_delay  -clock <name> … [get_ports <port>]
set_output_delay -clock <name> … [get_ports <port>]
set_max_delay <v> -datapath_only -from <clocks|objects> -to <clocks|objects>
set_case_analysis <0|1|zero|one|rising|falling> [get_ports <port>]
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

`set_input_delay` and `set_output_delay` both land in
`ClockSpec.port_clock`; the port's direction decides the consumer. A
typed *input* is a crossing source in `find_crossings`; a typed
*output* is an external-capture contract checked by CDC-024 (§8.21); an
*untyped* output is CDC-025's (§8.22).

Delay targets are kept as written — a bus index stays on its name
(`{d[3]}` → `d[3]`) and globs stay unexpanded — in
`ClockSpec.port_delays` (one `PortDelay` per statement, file order) as
well as in `port_clock`. The parser has no netlist, so the CLI calls
`sdc_objects.resolve_port_delays(module, spec)` right after parsing,
before `synthesize_unconstrained_inputs`, and it rewrites `port_clock`
in place (rtl-buddy-cdc#357):

- An exact port name is unchanged; an SDC naming only whole ports is
  not touched at all.
- A **bus index** types the **whole port**. Port typing is per port
  in every consumer (crossing sources, CDC-006 / -011 / -024 / -025,
  the domain map), so per-bit typing would be a change to all of them;
  instead the selection is over-approximated, and a port whose bits
  end up on *different* clocks (`{d[0]}` on `clk_a`, `{d[1]}` on
  `clk_b`, or a bit retyped after a whole-port typing) gets a warning
  naming them and takes the last clock.
- A **glob** (`*`, `?`) is matched with the object-pattern matcher
  (`NameIndex.ports`, so `*` never crosses a hierarchy level and `d*[3]`
  works) and selects only ports of the command's direction (inputs /
  inouts for `set_input_delay`, outputs / inouts for
  `set_output_delay`) that no `create_clock` declares a clock on.
- A target matching nothing is warned about and kept as written, so it
  types nothing, as before.

At the end of parsing, each delay whose `-clock` names no declared
clock (`create_clock`, virtual or not, or `create_generated_clock`) gets
a `partial_warnings` entry; deciding after the whole file lets a clock
be declared below the delay that uses it.

Generated clocks fold back into their master via `ClockSpec.resolve`
unless an explicit `set_clock_groups -asynchronous` overrides the
relationship. When a `create_generated_clock` target is a `[get_pins
<hier_pin>]` expression rather than a top-level port, the pin path is
recorded in `ClockSpec.pin_clocks` and consumed by the clock walker
(§5.1) — that's the integration point for SoC clock-forwarding
chains. `set_false_path -from [get_clocks A] -to [get_clocks B]` is
treated as a pairwise async hint (equivalent to async groups for that
specific pair). A `set_false_path` with any design-object endpoint or a
`-through` is something else entirely — an acknowledged path, never a
clock pair — and is §6.4's subject. Exclusive groups drop crossings as unreachable in
`_filter_async` before any rule sees them. `set_max_delay
-datapath_only` between two clocks is an async hint that also
overrides a same-root collapse and an exclusive group (§4.5).
`set_case_analysis` on top-level ports resolves case-analysed clock
muxes in the clock walker (§5.2). A second `create_clock` on a port
replaces the first unless `-add` is given; with `-add` the port carries
both (§4.5, "Multi-clock ports"). Bus bits must be braced
(`[get_ports {sel[3]}]`): unbraced, the Tcl backend evaluates `[3]` as
command substitution and the target becomes the whole bus, so the
reader warns on any command whose name is a number.

### 6.2 What it deliberately ignores

STA-only commands (plain `set_max_delay` — only the
`-datapath_only` form carries CDC intent — `set_min_delay`,
`set_load`, `set_drive`, `set_disable_timing`, …) are silently
dropped at the `logging.DEBUG` level. This is by design:
users should be able to point the tool at their existing constraint
file without curating a CDC-only subset.

The parser is also deliberately *not* a Tcl interpreter (see this
section's intro). Constructs not supported: command substitution
beyond `[get_clocks …]` / `[get_ports …]` / `[get_pins …]`, `set`
variables, `expr`, and `-filter` clauses inside collection commands. If any of these become real requirements, the right move is
to switch to `tkinter.Tcl()` (the rejected alternative recorded on
#144), not to grow them onto this tokenizer.

### 6.3 Diagnostics policy

The silent-drop default is tempered by `ClockSpec.partial_warnings`:
when the parser sees a CDC-relevant command it can't fully
understand (e.g. `set_clock_groups` without a kind specifier,
`[get_clocks -filter …]`, a `-filter` inside an object-scoped
`set_false_path`), it appends a one-line description. The CLI adds the
netlist-side diagnostics to the same list before printing it: case
analysis on a non-port, and an SDC object pattern that matches nothing
(§6.4). The
CLI surfaces these once at the end of parsing (in text mode, to
stderr) rather than logging line-by-line — keeping the noise floor
low for the common case while flagging cases where the user might
assume CDC coverage that isn't there.

Truly unrecognised commands (`set_load`, `set_drive`, …) emit only
a `logging.DEBUG` line so they're visible under `--verbose` but
don't pollute normal output.

### 6.4 Object-scoped exceptions (`set_false_path` objects / `-through`, issue #328)

A clock-pair `set_false_path` makes a clock pair *asynchronous* — more
checking. An object-scoped one (`-from [get_cells u_cfg/mode_q_reg*]`,
`-through [get_pins u_cfg/mode_o]`) does something different: it removes
the timing check on specific paths. It **does not add a synchroniser**,
and it never says anything about the clocks as a whole. The analyzer
therefore reads it as a *declared, reviewed exception* on the crossings
it covers: those crossings stay in the report, and only the findings the
exception actually answers are moved to the suppressed list, with the
SDC statement as the justification.

**Layering.** `sdc.py` stays a pure parser: `_handle_set_false_path`
sends any statement with a `get_cells` / `get_pins` / `get_ports` /
`get_nets` endpoint or a `-through` to
`ClockSpec.false_path_exceptions` as a `FalsePathException`
(`SdcObject(kind, name, hierarchical)` patterns per side, clock
endpoints given alongside them, one tuple per `-through` flag, the edge
/ check `qualifiers`, the source `line`, and a canonical `statement`
rendered from the record so it is identical on both backends). A word
with no object collection in it is read exactly as before (every name a
clock), so clock-pair false paths are byte-for-byte unchanged, and a
`FalsePathException` never feeds `are_async`. The netlist side lives in
`sdc_objects.py`, called from `cli._analyze_module_and_report`:
`resolve(module, spec)` right after the other SDC diagnostics,
`coverage(...)` once the crossings exist, `acknowledge(...)` after the
waivers. `set_max_delay -datapath_only` object endpoints
(`DatapathOnlyDelay.from_objects` / `to_objects`) go through the same
resolver.

**Name matching** (`sdc_objects.NameIndex`, built once per design;
tables are built lazily, every pattern is resolved once and cached, an
exact name is a dict hit and a glob scans only the sorted-key slice
sharing its literal prefix). An SDC name is normalised first: `\`
escapes dropped, the `/` hierarchy separator becomes `.` (the separator
both frontends use). `*` / `?` never cross a hierarchy level;
`get_* -hierarchical` matches the pattern at any depth. `[…]` is a
literal bus index.

| Object | Matches | Does not match |
|---|---|---|
| register (`get_cells`, a pin's owner) | every non-internal (no `$`) netname on the flop's `Q` whose scope is the flop's own instance or a generate scope inside it — the cell is `$flatten\u_cfg.$procdff$8` (Yosys) or `u_cfg.$slang$dff$3` (slang), neither the register's name, while both name `Q` `u_cfg.mode_q`; each name bare and with `_reg` on its leaf (`u_cfg/mode_q_reg`, `u_sync/chain_reg[1]` for an unpacked element); `[i]` selects one bit, numbered by the net's declared range (`offset` / `upto`, now loaded on `Netname`); a cell whose own name is meaningful (a read-back gate-level netlist) | a parent's wire the flop drives through a port (`w` for `u_cfg.mode_q`), a child instance's port it feeds (`u_sub.d`); synthesis renames (`mode_q_reg_3_`, a generate scope flattened to `g_1__r`). An output port written straight from the register is the same net, so it is an alias (`u_cfg/mode_reg` names `u_cfg.mode_q`) — over-match by design |
| pin (`get_pins`) | any pin of a register names that register (`…/Q`, `…/D`, `…/CK`); otherwise the hierarchical pin of a flattened instance, i.e. the net of that name (`u_cfg/mode_o` → `u_cfg.mode_o`) | pins of anonymous combinational cells |
| port (`get_ports`) | top-level ports, `[i]` per bit | — |
| net (`get_nets`) | netname bits; as `-from`, the flop / input port driving them; as `-to`, the flops whose `D` they feed | internal `$` names |
| `-through` | net bits of nets, hierarchical pins, a register's `Q` / `D` pins, ports; a bare name tries net, pin, port | cells (statement inert, warning) |

A pattern that resolves to nothing usable is a warning naming it
(`set_false_path (line 12): -from cell pattern 'u_cfg/mode_qq_reg*'
matches nothing usable in the netlist — constraint typo?`), once per
distinct message.

**Coverage** (`sdc_objects.coverage`). A crossing is covered when
every (source bit, destination bit) pair it can carry is named by some
statement's `-from` × `-to` — so a register constrained one bit per line,
as synthesis-generated SDCs write it, is covered by the lines together,
and three of four bits covers nothing. Bits are counted over the whole
source / destination flop (conservative: the crossing record does not say
which source bits reach the destination). A clock endpoint covers its
side when every clock the side's domain stands for
(`colocated_clocks`) is listed; an absent flag covers anything. A
statement with `-through` must cover alone, and only when **every** path
from source to destination passes each `-through` group: the crossing
walk is re-run from the source (same cells, lane-precise edges, same
4-hop budget) with the through bits as cut points, and the destination's
`D` must become unreachable. A partial cover counts for nothing.
False-path and datapath-only statements are evaluated separately.

**Effect on findings.** A covered crossing is *acknowledged* only by a
false path with no edge or check qualifier (a `-rise_from` or `-setup`
false path leaves the other edge / check timed) that names the
crossing's **source by object** — `-from [get_cells …]` /
`[get_pins …]` / `[get_ports …]` / `[get_nets …]` — or pins the path
with `-through`. That is the SDC-side equivalent of asserting the source
quasi-static. A `-to`-only or clock-sourced object false path (the usual
`set_false_path -to [get_cells */sync_reg[0]]`, which marks a capture
point and says nothing about the source) covers the crossing for the
domain map but acknowledges nothing. An acknowledged crossing loses
exactly the findings `(* cdc_static *)` silences for its source:

| Rule | Acknowledged? | Why |
|---|---|---|
| CDC-001 unsynchronised control | yes | a static source cannot be sampled mid-transition |
| CDC-002 short synchroniser | yes | same — depth guards a toggling source |
| CDC-003 comb before synchroniser | yes | the glitch needs a toggling input; per-crossing, so another, unacknowledged source through the same gate still fires on its own crossing |
| CDC-004 multi-bit bus | yes | coherence of a bus that does not change |
| CDC-019 one-hot decode | yes, only if **every** lane's crossing is acknowledged | stricter than `cdc_static`, which silences the group when any lane is static |
| CDC-020 sliced bus | yes, only if every crossing out of the register is acknowledged | same |
| CDC-005 reconvergence | **no** | the hazard is at the destination's recombination; a static source merged with a live one is still a coherency hazard (`cdc_static` keeps it too) |
| CDC-006 port comb → sync, CDC-011 unconstrained input | **no** | `cdc_static` has no port form; CDC-011 is a missing-constraint finding whose fix is `set_input_delay` |
| CDC-012 data-hold, CDC-013 fast→slow event loss, CDC-009 pulse width | **no** | protocol findings on gated / pulse crossings; not in `cdc_static`'s set |
| CDC-014 / -015 / -016 / -017 / -018 / -022 | **no** | a broken or degraded synchroniser stays broken whatever the source does |
| CDC-008 / -010 / -021 / -023 | **no** | clock-network findings, not data crossings |
| RDC-* | **no** | a false path removes a timing check, not a reset synchroniser — `set_false_path -from [get_ports rst_n]` is routine and must not hide RDC findings |

`DatapathOnlyDelay` object endpoints are resolved and covered the same
way but acknowledge nothing: a bounded datapath still needs a
synchroniser. They only annotate the domain map.

**Representation.** An acknowledged finding is a `SuppressedViolation`
— the waiver channel (§9), applied **after** the waiver file and pragmas
so an existing waiver keeps the findings it already claimed. Its
`Waiver` carries `sdc: SdcJustification(statement, line, file)`,
`rule_pattern` = the rule id, `regex` = the escaped cell name,
`reason` = `"SDC false path: <statement>"` (plus `(+N more)` when
several statements cover it together), `source_line` = the SDC line
(`0` on the tokenizer backend, which tracks none):

- **text** — listed under the heading `Suppressed by waivers and SDC
  exceptions` (the heading is unchanged when no SDC entry is present),
  provenance `(sdc <file>:<line>)`;
- **JSON** — a `suppressed[]` entry like any other, its `waiver` object
  gaining `sdc: {statement, line, file}` (only on these entries); counted
  in `summary.suppressed`;
- **SARIF** — a result with `suppressions: [{kind: "external", status:
  "accepted", justification: "SDC false path: …"}]`;
- **exit code** — unaffected, as for any suppressed finding;
- **domain map** (schema 1.3, additive) — top-level
  `false_path_exceptions` (every record, sorted), and on each covered
  crossing `sdc_exceptions: [{command, statement, line}]` plus
  `sdc_acknowledged: bool`, both absent elsewhere. An acknowledged
  finding is not in that crossing's `findings` (which lists raised
  findings), exactly like a waived one.

**Out of scope.** Findings lifted from inside an abstracted blackbox
(#261) are not matched against SDC objects (their flops are not in the
flat netlist). `-filter` inside an object collection drops the whole
statement with a warning — dropping just the filter would widen its side
to "any object". Ordering of several `-through` groups is not modelled
(each must be passed; the order is not checked).

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
  port, no `Crossing` record is emitted — the receiving flop is outside
  the netlist. An output that carries a `set_output_delay -clock`
  contract is checked instead by CDC-024 (§8.21), which walks the
  output's cone directly; it deliberately does **not** fake a sink
  flop, so no crossing-consuming rule ever sees an external capture.
  An output with no contract is reported by CDC-025 (§8.22) when it
  merges asynchronous domains, or — once the SDC types some other
  output — as a missing contract; otherwise the responsibility shifts to
  the consumer of the port.
- **Untyped-port crossings, unless the sentinel was synthesized.**
  The walk only visits ports present in `port_clock`. The CLI calls
  `sdc.synthesize_unconstrained_inputs` first, which stamps
  `UNCONSTRAINED_SENTINEL` on every input port the SDC left untyped —
  so in the normal pipeline untyped ports *are* walked, and their
  crossings carry the sentinel as `src_clock` for CDC-011 to consume
  (CDC-001 / CDC-002 / CDC-006 skip them to avoid double-firing with
  mismatched fix advice). A caller that skips the synthesis step gets
  no port-sourced crossings for untyped ports at all.
- **CLK pins as transit bits.** `_build_bit_consumers` filters out
  clock connections so the data BFS doesn't follow them.
- **Reset and control pins as crossing *sinks*.** `flop_by_d_bit` is
  built from `Flop.d`, so the only pin the BFS can terminate on is
  `D`. A signal arriving on `SRST` / `ARST` / `EN` / a clock-mux `S`
  never produces a `Crossing`, no matter which domain it came from.
  This is a real scope boundary, not an oversight — the rules that
  consume crossings (sync-chain depth, gray coding, bus width,
  reconvergence) all reason about a *data* path, and a reset pin is
  not one. Rules that care about those pins walk them directly
  instead: RDC-001 / RDC-003 from `ARST` / `SRST`, CDC-010 from
  clock-network control pins, and CDC-011 from `SRST` (§8.18). When
  adding a rule about a non-`D` pin, walk the pin — do not widen the
  crossing model.
- **Same-domain crossings.** Filtered out at record-creation time —
  if `dst_clock == src_clock`, no record is created.
- **The storage path of a memory.** Memory *ports* are walked (§7.5),
  but write data is never propagated to read data: that path is
  CDC-026's subject (§8.21), because the sanctioned async FIFO crosses
  it without a synchroniser by design.

### 7.4 Boundary re-seeding and the compositional walk

When the design carries auto-abstracted blackbox boundaries (§4.8),
`find_crossings(module, boundaries=...)` re-seeds the boundary crossings
the flattened subtree would have produced, on both sides:

- **Output side (virtual sources).** A third pass alongside the
  flop-source and port-source walks: for each boundary instance and each
  summarised *output* port it seeds a BFS frontier at the port's
  connected bits — exactly the port-walk shape — and emits a
  `src_boundary = (instance, port)` `Crossing` for every foreign-domain
  sink it reaches. `synchronised=True` on an *output* does not suppress
  the pass (§4.9 / §4.10: the port carries the domain that really drives
  it, so a sink in a third domain is still a crossing); a
  `src_clock=None` port seeds the `<unconstrained>` sentinel so it
  crosses to any known-domain sink.
- **Input side (virtual sinks, P3 / #257).** For each summarised *input*
  port a synthetic sink flop standing in for the boundary input pin is
  folded into the domain map (`D` = the port's connected bits, clock =
  the port's own `PortBoundary.src_clock`, falling back to
  `BoundarySummary.clock`; for a single-clock summary those are the same
  value on every port, and for a #261 multi-clock summary the per-port
  domain is what makes the collapse per-domain rather than one hub). The
  flop-, port-, and output-boundary walks
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
a genuine multi-clock subtree carrying an internal crossing — or, per
#273, a module whose output drives a clock, recorded in
`clock_output_ports`) is absent from the map, so its instances fall
through to the normal flat walk over whatever internals the netlist
actually contains.

> **Per-instance keying (#257).** The boundary map is keyed by instance
> path, and summarisation is cached by `(module type, clock context)`.
> So a module instantiated N times *in the same clock domain* is still
> analysed once (the cache hit — the epic's "analyse once" goal), while
> the same module type instantiated under *two different* clock domains
> gets a correct per-instance summary for each: each instance's boundary
> crossings are seeded against the domain its parent actually drives.
> This replaces the earlier type-only keying, which could only represent
> one domain per module type.

`compose_boundaries` also drives the **compositional per-module pass**
(§4.9, #261) for every greybox sibling: one `compositional.analyse_module`
call per `(module type, clock context)`, its result folded into the
summary and its internal findings collected in `CompositionStats.lifted`
for the CLI to report once. `stats.analysed_modules`,
`ambiguous_input_modules` and `unresolved_internal_modules` say which
modules took that path and, for the residual declines, which reason the
`CDC-BBX` message should give.

The end-to-end safety property is **parity**: on a design small enough
to run both ways, the auto-abstracted run (subtree blackboxed +
summarised once) must produce the *same* violations and the same
`summary.*` counts as the fully flattened run, while walking strictly
fewer flops.

> **What parity means for a greybox (#261).** Two relations, because a
> boundary changes *where* a finding is anchored, not whether it exists:
>
> - **Findings anchored in the parent** — every crossing that touches a
>   boundary port — are **equal** to the flat run's: same rule ids, same
>   severities, same crossing count. `bbx_input_sync` (clean both ways),
>   `bbx_input_sync_broken` (2 × CDC-001 both ways) and
>   `reconvergence_two_inputs` (1 × CDC-005 both ways) pin this.
> - **Findings anchored inside a boundary module** are equal **up to
>   per-module-type deduplication**: N identical instances yield one
>   lifted finding naming N instances instead of N copies
>   (`bbx_shared_internal_violation`: 2 × CDC-002 flat, 1 × CDC-002
>   lifted). That is the analyse-once contract, not a loss.
>
> Neither relation is a subset in the hazard sense — nothing that fires
> flat goes unreported — with two stated exceptions: the three top-level
> input-port rules §4.9 excludes from the internal pass (CDC-006 / CDC-011 /
> RDC-008), whose subject is reported by the parent at the boundary port
> instead; and the output rules CDC-024 / CDC-025 (§8.21, §8.22), which
> cannot see a top-level output driven *through* an abstracted boundary
> pin (no launching `Q` in the parent's cone). Two counts deliberately do **not** match: `summary.crossings`
> (an internal crossing is counted inside the module, not at top level)
> and `summary.flops` (the scaling win). And a rule measuring a structure
> that *spans* the boundary — CDC-018's chain length — sees each side
> separately. The `single_clock_leaf_abstract`,
`shared_subtree_compose`, and `foreign_input_no_abstract` fixture pairs
pin this — `shared_subtree_compose` proves a single-clock subtree
instantiated twice is analysed once yet yields identical CDC-004 findings
on the real top-level destination flops, and `foreign_input_no_abstract`
proves a foreign-domain signal driven *into* an abstracted subtree's
input still surfaces its CDC-004 (anchored on the boundary input pin via
`dst_boundary`) — identical to the flattened run.

### 7.5 Inferred memories (issue #326)

Yosys keeps an inferred RAM as memory cells, not flops, and the walk
above could not cross them — a dual-clock RAM, the storage of almost
every async FIFO, was invisible to every rule (the #176 coverage-gap
sentinel). What reaches the analyzer, checked on yosys 0.64 (rtl-buddy
fork):

| Flow | Cells |
|---|---|
| `lint` (`read_verilog` or `read_slang`; `proc; flatten; opt_clean`) | one `$memwr_v2` per write port; one **asynchronous** `$memrd` (`read_verilog`) / `$memrd_v2` (`read_slang`) per read, `CLK_ENABLE=0` — a registered read is that port feeding an ordinary flop; `$meminit_v2` for initialisers |
| `memory_dff` | the same, with an output register merged into a **clocked** `$memrd_v2` where it can be (`CLK_ENABLE=1`, `ARST` / `SRST` / `EN` / `CE_OVER_SRST` …) |
| `memory_collect` / `memory -nomap` | one `$mem_v2` per memory, every per-port field packed side by side: `WR_CLK` / `RD_CLK` one bit per port, `WR_ADDR` / `RD_ADDR` `ABITS` per port, `WR_DATA` / `WR_EN` / `RD_DATA` `WIDTH` per port, `RD_EN` one bit per port, and per-port flags (`WR_CLK_ENABLE`, `RD_CLK_ENABLE`, …) as bit strings, port *i* the *i*-th character from the right |
| `memory_map` | plain flops and muxes — ordinary flop crossings, out of CDC-026's scope |
| slang frontend | mirrors `lint`: a run-time-indexed write to a one-dimensional unpacked array in `always_ff` lowers to `$memwr_v2` (`EN` = the enclosing conditions), a run-time-indexed read anywhere to an asynchronous `$memrd_v2`; constant-indexed selects stay bit selects. Before #326 the write was dropped and the read became an opaque `$_UNKNOWN_` |

`memory.find_memories` decodes every form (v1 `$memwr` / `$memrd` /
`$mem` too) into one `Memory` per `MEMID` with its `MemPort`s, in one
pass over the cells; `$meminit*` is constant initialisation with no
clock and is ignored. An unclocked *write* port has no domain and is
left out, like a flop whose clock does not resolve.

`find_crossings` walks memories through `memory.crossing_view`, which
removes the memory cells from a copy of the module and stands in for
each port with something the existing walk already understands:

- a **write port** becomes a virtual sink flop (`$mem_port`, `D` =
  address + data + enable, no `Q`) in its write-clock domain;
- a **clocked read port** becomes a virtual flop with `D` = address +
  enable and `Q` = read data — a sink *and* a source in its read clock;
- an **asynchronous read port** becomes a transit cell
  (`$mem_async_read`, `A` = address + enable → `Y` = read data), so a
  foreign-domain address reaches whatever registers the read data
  exactly as through a mux.

Port clocks are resolved by `domain.bit_clock_resolver` — the same
tracer, generated-clock pins and canonical names `assign_domains`
uses. Virtual flops are named after the port cell (`<cell>[wr<i>]` /
`<cell>[rd<i>]` for a packed `$mem_v2`), so findings anchored on them
resolve the cell's `src` and instance path; like the blackbox
`$boundary_sink` they are never in `Module.cells`, and their type is
not an FF type. The consequence for the rule pack is that **memory
ports are ordinary crossing endpoints**: a wrong-domain write address
is an unsynchronised multi-bit crossing into the write port (CDC-004;
`bad_ram_write_addr_wrong_domain`), with no memory-specific rule. A
design without memory cells pays one type check per cell and walks the
unmodified module — every pre-#326 fixture report is byte-identical.

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
| `_bit_reader_count(module)` | `_build_context` (cached as `ctx.reader_counts`), every single-reader test above and in `compositional` | The one definition of "who reads this bit": cell **input** pins except `CLK` (a pin `port_directions` marks `output`, or `Y` / `Q` when the cell has no directions, is a driver, not a reader — so a blackbox's `clk_out` / `dout` never counts), plus every module **output / inout port** bit. A synchroniser stage exported straight to a port therefore has a second reader and ends the chain, exactly as it would through a buffer (rtl-buddy-cdc#344) |
| `_packed_shift_register_depth(head, reader_counts, module, bit_drivers)` | `_sync_chain_depth` | Recognise a synchroniser packed into one multi-bit flop (`s <= {s[N-2:0], d}`): `D` must be, lane for lane, either one of the flop's own `Q` bits or — for exactly one lane — an external bit; follow the per-lane shift and return the effective depth (#264). With a **synchronous reset** the lane test runs on the data leg of the `$mux` `proc` leaves on `D` (#301) |
| `_constant_leg_reset_mux_data_bits(head, module, bit_drivers, reader_counts)` | `_packed_shift_register_depth`, CDC-003 | Return the data leg of a single `$mux` whose `Y` is exactly the flop's `D` vector and whose other leg is entirely constant (either polarity, any reset value) — i.e. an un-folded synchronous reset. Rejects enable muxes (other leg = own `Q`), any other live leg, both-constant legs, `$pmux`, chained muxes, and a `Y` with more than one reader (#301) |
| `_srst_mux_data_bit_to_single_bit_flop(module, d_bit_to_single_bit_flop, bit_drivers, reader_counts)` | `_build_context` (cached as `ctx.srst_mux_data_bit_to_single_bit_flop`), `_sync_chain_depth`, `_sync_chain_flops`, `_chain_has_inter_stage_comb` | Reverse index from a constant-leg sync-reset `$mux`'s *data-leg* bit to the 1-bit flop it feeds. The chain walkers consult it when the `D`-bit lookup misses, so a separate-flop chain steps over each stage's own reset mux (depth 2 for `if (rst) … else begin s1 <= d; s2 <= s1; end`), and the inter-stage-comb detector skips that mux so CDC-014 matches the slang `$sdff` verdict (#304) |
| `_crossing_enters_through_reset_mux_only(module, crossing, ctx)` | CDC-003 | True when the only cell between the source flop and the destination flop's `D` is that flop's constant-leg sync-reset `$mux` — part of the flop, not logic on the crossing path. Keeps the yosys frontend in step with slang, which emits `$sdff` for the same source (#86/#301) |
| `_sync_chain_flops(module, head, ...)` | CDC-005 | Same walk as `_sync_chain_depth`, but returns the ordered tuple of chain flops so callers can reach the chain's tail without re-walking |
| `_backward_fanin(module, start_bits, drivers, ...)` | CDC-006 | Reverse-BFS from a flop's `D` through comb cells; returns the set of upstream flop names and input-port names reached |
| `_comb_driven_bits(module, bits, drivers)` | CDC-006 | The bits a genuine combinational cell drives, looking back through single-input buffers / inverters. CDC-006 walks `_backward_fanin` from these only (after stepping over the stage's own constant-leg sync-reset `$mux`), so a port wired straight to a synchroniser's `D` is an ordinary port-sourced crossing, not a glitchy comb source (rtl-buddy-cdc#360) |
| `_forward_reachable_flops(module, start_bits, consumers, ...)` | available for rule writers | Forward analog of `_backward_fanin`: returns the set of *flop* cell names whose `D` is on the cone |
| `_forward_reachable_cells(module, start_bits, consumers, ...)` | CDC-005 | Forward walk that returns *every* cell on the cone — flop or comb. CDC-005 uses this for reconvergence detection so a comb-cell recombination (sync outputs into an OR driving an output port) still counts |
| `_is_multibit_sync_first_stage(...)` | CDC-004 | Verify the destination is a width-N flop whose Q exactly equals another same-domain flop's D lane-wise |
| `_is_gray_encoded_source(...)` | CDC-004 | Backward-walk from src D, looking for the canonical `g = b ^ (b >> 1)` XOR pattern |
| `_is_gated_bus_crossing(...)` | CDC-004 | Recognise handshake-style gating — D updates only when an enable from the dst domain has been synchronized across. Three shapes are accepted: (1) `$mux` directly driving `D` with a dst-domain `S` (the original handshake), (2) the same mux behind up to `_GATING_BUF_BUDGET`=2 transparent fanout buffers (`$buf`/`$_BUF_`/`$_NOT_`/`$not`/`$pos`), (3) destination cell is a `$dffe`-style flop with a dst-domain `EN` fanin |
| `_trace_through_bus_buffers(module, bit, drivers, ...)` | CDC-004 | Walk one D bit backward through up to `_GATING_BUF_BUDGET` transparent single-input buffers and return the surviving upstream driver. Used by `_is_gated_bus_crossing`'s shape-2 path so Yosys-inserted fanout buffers don't hide the originating mux |
| `_clock_network_cells(...)` | CDC-008, -010 | Identify cells whose output transitively drives any flop CLK. CDC-008 *exempts* them ("clock as data" doesn't apply to the distribution itself); CDC-010 *targets* them (a wrong-domain control pin on one of these cells is the failure mode) |
| `_control_pins_for(cell, *, use_heuristic=True)` | CDC-010 | Classify a clock-network cell's *control* pins (those whose transitions can glitch the output clock). Three resolution paths, tried in order: (1) explicit map for Yosys higher-level cells (`$mux.S`, `$dffe.EN`, `$dlatch.EN`) and the gate-level mux family (`$_MUX_` / `$_MUX{4,8,16}_`); (2) prefix paths for the gate-level latch and enable-flop families (`$_DLATCH*` / `$_DFFE_*` / `$_SDFFE_*` → all carry the enable on `E`); (3) a conservative pin-name heuristic — input pins named `E` / `EN` / `CE` / `GATE` / `SE` (case-insensitive) on any other cell are treated as control. `use_heuristic=False` (CLI: `--cdc-010-no-heuristic`) suppresses the heuristic only; the map and prefix paths remain active. Returns the empty set for non-glitch-producing cell types ($buf / $not / AND-trees / vendor cells that lack a heuristic-matching pin) so the rule's outer loop short-circuits |
| `_clock_input_domains_for(module, cell, ctx, clock_spec, control_ports)` | CDC-010 | Set of clock-domain names that drive a clock-network cell's *non-control* inputs. Walks each non-control input backward through combinational cells; records the domain of any flop's Q reached (via `ctx.domains`) and the SDC clock name of any top-level clock port reached directly. Empty set means none of the inputs trace to a classifiable clock — the rule then stays silent (false-negative-biased) |
| `_output_cone(module, start_bits, drivers, input_bit_port)` | CDC-024, -025 | Backward combinational cone of a top-level output: the cells whose `Q` launches it, the top-level input ports it reaches (feed-throughs), and the bit set visited. Same walk shape as `_backward_flop_fanin` (each comb cell expanded once, depth 12) but keeps the ports and bits that walk discards; reads input ports from a prebuilt bit→port index rather than `Module.port_of_bit` (a linear scan) |
| `_reached_output_bits(module, seeds, cone, consumers, port_bit_index)` | CDC-024, -025 | Forward replay confined to that cone, lane-aware via `domain._lane_targets`, returning which of the output's bits the given launchers reach — so a bus reports "2 of 4 bits", not the whole bus, and CDC-025 can tell a per-bit merge of two domains from a bus whose halves merely sit side by side |
| `_output_launch_index(module, clock_spec, ctx)` | CDC-024, -025 | The run's `_OutputLaunchIndex`, built on first use and cached in `_RuleContext.output_launch_memo` (keyed by the `ClockSpec`): each output's `_output_cone` (walked at most once per run however many output rules ask), the memoised `flop_domain_resolver`, the bit→input-port index, and the `canonical` / `is_async` predicate `filter_async` applies. A plain mutable memo by design — the one non-frozen object on the rule context, invisible outside the two rules |
| `domain.flop_domain_resolver(module, pin_clocks, clock_for_port=, declared_clock_for_port=, max_depth=, drivers=, case_analysis=)` | `assign_domains`, CDC-024, -025 | The per-flop domain function `assign_domains` applies: `flop_clock_root_resolver`'s raw root put through `clock_for_port`. CDC-024 / -025 read the rule context's `domains`, which is built from the same inputs (`ClockSpec.clock_root_inputs()`, rtl-buddy-cdc#358); the resolver is their fallback for a context built for another spec |
| `domain.flop_clock_root_resolver(...)` | `_build_context`, `flop_domain_resolver` | The raw root of the same walk, before normalisation. The rule context traces each flop once and keeps both: `clock_roots` (CDC-021) and `domains` |
| `user_sync_flop_names(module)` | CDC-001..-003, -006 | Return cell names of flops the user has annotated `(* cdc_sync *)` |
| `user_gray_flop_names(module)` | CDC-004 | Return cell names of source-side flops the user has annotated `(* cdc_gray *)` |
| `scan_mode_port_names(module)` | §8.20 scan filter | Return the names of top-level **input ports** annotated with one of `SCAN_MODE_ATTRS` (`scan_en` / `scan_mode` / `test_mode` / `dft_scan_en`, matched case-insensitively). Port-scoped, not flop-scoped |
| `scan_mode_clock_select_flops(module, ...)` | §8.20 scan filter | Return cell names of flops whose `CLK` network is steered by a scan-mode port. Supplies the `is_scan_control` predicate to `domain.find_scan_mode_flops`, which reads the fact off the ordinary clock-root walk rather than re-deriving the clock network |
| `user_static_flop_names(module)` | CDC-001..-004 | Return cell names of source-side flops the user has annotated `(* cdc_static *)` — runtime-constant config/mode registers; suppresses CDC-001..-004 on crossings sourced from the tagged flop |
| `cells.parse_gate_storage_cell(cell_type)` | `flops.py` accessors, CDC-010 | The one decoder of Yosys gate-level flop / latch type names (rtl-buddy-cdc#336): clock edge, async/sync reset on pin `R` with its polarity and value, set pin `S`, async-load pin `L`, enable pin `E`, for `$_DFF_`, `$_DFFE_`, `$_SDFF_`, `$_SDFFE_`, `$_SDFFCE_`, `$_DFFSR_`, `$_DFFSRE_`, `$_ALDFF_`, `$_ALDFFE_`, `$_DLATCH_`, `$_DLATCHSR_`, `$_SR_`. Letter positions follow the Yosys cell library (`help <type>`); `tests/test_cells.py` cross-checks them against the local Yosys's descriptions. Returns `None` for any malformed suffix rather than guessing |
| `flop_reset(cell)` / `flop_reset_bits(cell, *roles)` / `flop_enable_pin(cell)` / `flop_clk_polarity(cell)` (in `flops.py`) | `reset_domain`, RDC-001..-006, CDC-004, CDC-011, CDC-015, CDC-016 | Read a flop's reset pin (role `arst` / `srst` / `clr` / `aload`, pin, kind, polarity, reset value), enable pin (`EN` / `E`) and clock edge the same way for coarse cells (parameters) and gate-level cells (type name). A rule scoped to one coarse pin (RDC-001 reads `ARST`) asks for that role, so it stays scoped on a gate-level netlist |
| `is_ff_cell(cell_type)` (in `flops.py`) | every rule + `find_flops` | Predicate "is this Yosys cell type a flip-flop?" — true for higher-level `$dff*` families AND for gate-level cells that `cells.parse_gate_storage_cell` decodes as clocked (`$_DFF_`, `$_DFFE_`, `$_SDFF_`, `$_SDFFE_`, `$_SDFFCE_`, `$_DFFSR_`, `$_DFFSRE_`, `$_ALDFF_`, `$_ALDFFE_`). The two families have different pin name conventions (CLK vs C); use `flop_clk_pin(cell)` to extract the clock portably. Replaces direct `cell.type in FF_CELL_TYPES` checks across the codebase to make rules work uniformly on pre-`abc` and post-`abc` netlists |
| `flop_clk_pin(cell)` (in `flops.py`) | `find_flops` + `domain.trace_clock_root` | Return the clock-pin connection (tuple of `Bit`s) for a flop cell, portably across families. Higher-level cells use the ``CLK`` pin; gate-level cells use ``C`` |
| `is_latch_cell(cell_type)` (in `flops.py`) | CDC-017 | Predicate "is this a transparent latch?" — true for `$dlatch` (higher-level) and the gate-level `$_DLATCH_*` / `$_DLATCHSR_*` families |
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
   immediate driver of the reset bit is `$mux` / `$pmux` (or a
   gate-level `$_MUX_` family cell) with at least one *non-constant*
   data lane behind that bit. A mux whose data legs are all
   constants (`rst_n ? 1'b0 : 1'b1`) is an inverter or buffer of its
   select, not a choice between reset sources, and is skipped
   (rtl-buddy-cdc#354) — synthesis rewrites it into the `$_NOT_` it
   is, which RDC-006 never matched, so both netlists agree. The same
   cell gets no RDC-005 mux exemption either: it selects nothing.
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

On a gate-level netlist the decoder is one gate per lane, so no driver
has ≥2 output bits. A lane whose driver is a one-bit gate-level cell
(`$_*`) is grouped instead by the narrowest public multi-bit netname
holding its `D` bit — the decoder's output vector (`one_hot`), which
survives `techmap` and `abc` (rtl-buddy-cdc#337).

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
declared `Clock`). A flop whose raw clock root
(`ctx.clock_roots[name]`, §4.3) is the name of a top-level input port
that *isn't* in that set is flagged. It reads the raw root rather than
`ctx.domains` because the domain is normalised through
`clock_for_port`, which on the CLI path maps an undeclared (untyped)
port to the `<unconstrained>` sentinel. Generated clocks declared via
`[get_pins ...]` have roots that are clock names (not port names), so
they're filtered out by the port-membership check. The rule is skipped when no SDC is
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

Four checks:

1. **Same port in multiple clocks**: scan `Clock.ports` across every
   declared clock; flag any port that appears in ≥2 clocks.
   `clock_for_port` is deterministic (first match wins) but the
   user's intent is ambiguous. Since #330 a parsed SDC only reaches
   this shape through `-add`, which is legitimate and recorded in
   `multi_clock_ports`, so it is skipped; an accidental redefinition
   without `-add` is a *replacement*, warned inline by the handler. The
   check still guards specs assembled without `parse()`.

2. **Unresolved master**: for each generated clock with
   `Clock.master` set, check the master name exists in
   `spec.clocks`. An unresolved master makes `ClockSpec.resolve`
   return the unknown name unchanged, breaking `are_async`'s
   root-comparison.

3. **Master cycle**: walk each generated clock's master chain with
   a per-walk visited list; if a name is revisited, the chain is
   cyclic. `ClockSpec.resolve` has a cycle guard, but the SDC is
   methodology-broken.

4. **Generated clock on a multi-clock source** (#330): a
   `create_generated_clock -source` port in `multi_clock_ports` with no
   `-master_clock` (ambiguous — the generated clock is left without a
   master, as for any `-source`-only declaration), or with a
   `-master_clock` that names none of the port's clocks.

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
has sampled. The rule's detection signal is that ack, as an
**acknowledge path** reachable from *this crossing's* source flop.

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
4. The crossing's source flop has no acknowledge path
   (`_has_dst_to_src_feedback`).

**The acknowledge path** (rtl-buddy-cdc#359, #369) is a chain of
registers from a `dst_clock` register to the payload register's
**load / hold control**. `_has_dst_to_src_feedback` walks it backward
from the payload register (`crossing.src_flop`):

1. **Entry: the hold control only.** `_hold_control_bits` is the
   payload register's clock enable (`EN` / gate-level `E`) plus the
   select of every *hold mux* — a mux on a mux-data-leg path from `D`
   back to the register's own `Q` (`if (load) q <= x` lowers to
   `$mux(A=q, B=x, S=load)`). The walk starts from those bits'
   combinational fanin, never from the `D` value. A destination signal
   that only changes *what* is loaded — a mode bit synchronised into
   the source and steering `q <= m ? ~x : x` — holds nothing, and is
   not an acknowledge (`bad_datahold_dst_mode_bit`; before #369 any
   path into `D` silenced the rule).
2. **Then register to register** through `_flop_functional_fanin_bits`
   — `D`, the clock enable, a synchronous reset — never an asynchronous
   reset, set or load, so a register that hears the destination only
   through a shared reset carries no acknowledge. At most 6
   source-domain registers: the ack synchroniser and the request /
   busy flops the load condition reads. That is also how the request
   (or toggle) generator counts: when the payload's load is derived
   from the generator's state, the generator is in the hold-control
   cone and its own control is walked; a generator that is gated by
   the ack while the payload loads freely does not hold the payload,
   and CDC-012 fires.
3. **Then, if needed, through other domains.** An ack relayed
   destination → a third domain → source
   (`good_handshake_ack_via_third_domain`: clk_b's ack is synchronised
   into clk_c, re-registered, and synchronised into clk_a) is followed
   through at most 6 registers of domains other than `src_clock`.
   Whether each relay hop is itself synchronised is CDC-001's job
   (`bad_handshake_ack_via_third_domain`: CDC-001 on the C → A capture,
   no CDC-012).

The walk reports feedback on reaching a `dst_clock` register, with one
visited set. Clock-network registers (dividers) and recognised reset
synchronisers (`_ack_walk_excluded`) are never traversed or accepted; a
register whose own fanin never reaches the destination (a free-running
register) ends its path. The feedback result is cached per
`c.src_flop.cell.name`, and each register's functional fanin is
computed once per run (`fanin_cache`). The domain map's `"handshake"`
verdict (`crossing_synchronizer`) uses the same walk.
`tests/test_cdc_012_relayed_ack.py` pins the relayed ack (clean), the
mode-bit reproducer (fires), the same bit moved onto the hold mux's
select (clean), and the reset-only and free-running relays (fire). Every
committed CDC-012 good fixture holds its payload through its load
control; none relied on a data-only path.

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

### 8.17 CDC-022 — CDC primitive with insufficient sync depth

Recognising a vendor CDC macro (§4.10) removes its crossing from the
report, which removes CDC-002's only handle on synchroniser depth for
that path. The depth did not disappear, though — it moved from a
walkable flop chain into an **instance parameter**: `DEST_SYNC_FF` on
every XPM CDC macro, plus `SRC_SYNC_FF` on `xpm_cdc_handshake` for the
returning ack. Without CDC-022 a project that sets `--sync-depth 3`
would silently accept every `DEST_SYNC_FF=2` instance in the design;
the rule restores the check at the only place the number is visible.

Detection is unusually simple, and deliberately so: `check_cdc_022`
reads `Cell.type` and `Cell.parameters` and nothing else. It therefore
fires whether or not a blackbox sibling module was loaded for the
macro — a macro that arrived as a bare unresolved cell type still gets
its depth checked, which is the common shape in a vendor-emitted
netlist.

Two defaulting decisions:

- Yosys records only **overridden** parameters on a cell, so an
  instantiation that takes XPM's default has no `DEST_SYNC_FF` entry at
  all. Staying silent there would make the rule useless on the most
  common instantiation style, so an XPM macro with the parameter absent
  is checked against UG974's documented default of **4**.
- A `--sync-primitive`-registered macro has **no** known default. An
  absent parameter there is skipped rather than assumed — we have no
  basis to invent a number for a third party's macro.

Severity is `warning` (`--strict` → error), mirroring CDC-002. A
2-stage synchroniser is correct engineering at most clock rates; the
finding is "shallower than *this project* asked for", not "broken".

CDC-022 is a **new rule id**. The rule-id set is a downstream contract
(waiver files, rtl-buddy-xeno's mutation-operator predictions,
`rb cdc` reporting), so ids are added, never renamed — the same
discipline the CDC-007 → RDC-001 rename had to pay for with a
legacy-id alias in `waivers.py`.

### 8.18 CDC-011 — the `SRST` supplement (issue #272)

CDC-011 is defined over *ports*, but it was implemented over
*crossings*, and crossings only sink on `D` pins (§7.3). That mismatch
made one finding depend on a synthesis-pass detail.

Take an untyped input used as a synchronous reset. If the consumer
flop also has a real data input, `proc` leaves a `$dff` whose `D` is a
`$mux` selecting the reset constant — the reset is on a `D` pin, the
port walk reaches it, CDC-011 fires. If the consumer has no external
data input (a self-toggling counter, say), or if `opt_dff` runs, Yosys
folds the same reset into a dedicated `$sdff` `SRST` pin — no `D`-pin
arrival, no crossing, no CDC-011. RDC-003 did not cover the gap
either: it compares the reset's *source domain* against the consumer
clock, and an untyped port's source domain is the
`<unconstrained>` sentinel, not a named clock.

The fix keeps the rule's port-level semantics and drops the crossing
list as the sole source of destinations. `_unconstrained_srst_captures`
walks every flop's `SRST` connection backward with
`_backward_port_fanin`, collecting `{port: {(dst_clock, dst_flop)}}`
for ports carrying the sentinel; `check_cdc_011` unions that with the
crossing-derived destinations **before** deciding severity. Unioning
first is what keeps the existing multi-domain escalation honest: a
port captured on a `D` pin in one domain and an `SRST` pin in another
is still one `error`, not two findings.

Scope is synchronous resets only. A sync reset is sampled on the
destination clock edge exactly like a data input, so "declare
`set_input_delay -clock`" is correct advice for it. An async reset pin
(`ARST` / `CLR` / `ALOAD`) is legitimately untimed — the same advice
there would be wrong — and RDC-001 / RDC-006 / RDC-008 already own the
async-reset failure modes.

The invariant to preserve: the same RTL and SDC must yield the same
rule id, severity, and message text under either lowering. The
`bad_untyped_sync_reset_srst` / `bad_untyped_sync_reset_mux` fixture
pair is the same source built with and without `opt_dff`, and
`tests/test_bad_untyped_sync_reset_srst.py` compares the two findings
sets directly.

### 8.19 CDC-023 — clock net driven by a combine of two declared clocks

Issue #263 made `trace_clock_root` **decline** when a clock gate or a
clock-path transparent latch combines two distinct declared clocks
(§5). That was a soundness fix: better a loud `domain_unknown` than a
silently mislabelled flop. But the decline is silent *about the cause*
— the flop just joins the generic under-resolution tally, and the user
has no way to tell "this block's clock tree is deeper than
`--clock-trace-depth`" apart from "someone ANDed two clocks together".
The second is a real design smell — a combinationally mixed clock
glitches, runs at no declared frequency, and every crossing behind it
goes unchecked — and it deserves a name.

**Structure: the finding is emitted by the decline, not re-derived
from it.** The obvious implementation is a rule that re-walks the
clock network looking for combining cells. That was rejected: it means
a *second* predicate for "is this a real declared clock, or just an
enable port", and the moment the two drift the rule either cries wolf
on every ICG or goes quiet on a real combine. The #267 hardening
already showed how subtle that predicate is — the `<unconstrained>`
sentinel that `synthesize_unconstrained_inputs` stamps on untyped input
ports must not count as a clock, or every port-enabled ICG wrongly
declines.

So instead:

- `_pick_combining_root` takes an `on_decline` callback, invoked at the
  one line that returns `None`;
- `trace_clock_root` / `_trace` thread an `on_combine` recorder
  (`domain.CombineSink`) that receives `(cell, cell_type, driven_bit,
  clocks)`;
- `domain.find_clock_combines` runs the ordinary per-flop walk — same
  `max_depth`, same drivers, same `_clock_identity_fn` predicate
  `assign_domains` uses — with the recorder attached, dedupes events per
  `(cell, driven bit)`, and returns `ClockCombine` records;
- `check_cdc_023` renders those records and nothing else.

The recorder observes the walk; it never changes its outcome. The
predicate itself was hoisted out of `assign_domains` into
`_clock_identity_fn` so there is literally one copy. Net effect: the
rule fires **iff** the tracer declined, by construction rather than by
two implementations agreeing, and the mux exclusion, the same-clock
(#166) exclusion, and the sentinel exclusion are all inherited for
free.

The cost is one extra clock-root pass over the flop list. It is skipped
outright when fewer than two clock identities are declared (no SDC, or
a single-clock design), which is the case where an extra walk would buy
nothing.

**Reporting.** One finding per combining node, not per stranded flop:
the node is what the user edits. The message names the combined net,
the declared clocks that met, and the cell, then a bounded sample of
the flops the node clocks (the count is always exact). `cell_name` is
the combining cell, so the JSON/SARIF source location points at the
gate and a waiver regex can match it.

**Severity is `warning`** (`--strict` → error). Combining two clocks is
nearly always a bug, but the shape also covers deliberate, characterised
test/debug clock chopping, and a new rule id that turns a
previously-clean run into a hard `error` the day it lands is the wrong
default for a diagnostic whose escape hatch is a waiver. Sites that
disagree get `error` via `--strict`.

**Note the one asymmetry.** A declined node does not *always* strand its
sinks: a decline on one leg of an upstream **mux** still lets the mux
resolve through its other leg. The combining node is a real smell
either way, which is why the report keys on the node rather than on
the unresolved flop — and why CDC-023's count and
`summary.domain_unknown` are related but not equal.

CDC-023 is a **new rule id**, added under the same discipline as
CDC-022: ids are added, never renamed or reused. 022 was the previous
maximum; no retired id was recycled.

Fixtures: `clock_combine_gate` (`$and` of two `create_clock` ports),
`clock_combine_latch` (`$dlatch` with `D`=clkA, `EN`=clkB), and
`clock_combine_generated` (a `create_generated_clock` target combined
with a real clock) fire; `icg_port_enable` — the #263 regression guard
— must stay silent, and `tests/test_cdc_023_clock_combine.py` loads
every fixture through `synthesize_unconstrained_inputs` so the sentinel
case is actually exercised.

### 8.20 DFT scan-mode suppression (`--ignore-scan-mode`, issue #45)

Not a rule — a **cross-rule crossing filter**, and the only one in the
pack. A DFT insertion flow wedges a clock mux in front of a flop's
`CLK` so the scan chain can shift on a dedicated tester clock:
`$mux(S=scan_en, A=func_clk, B=scan_clk)`. Structurally that mixes two
asynchronous clocks onto one net; the tracer resolves the muxed net to
one leg, and every data path landing there from the other leg's domain
reads as an ordinary unsynchronized crossing. Structurally the shape is
indistinguishable from a genuine runtime clock select, which *would* be
a real hazard, so the analyzer cannot separate them on its own.

The missing fact is external: which pin is the test-mode control.
`SCAN_MODE_ATTRS` (`scan_en` / `scan_mode` / `test_mode` /
`dft_scan_en`, matched case-insensitively) names it, and
`rules.scan_mode_port_names(module)` reports the top-level **input
ports** carrying it. Ports only, mirroring
`user_reset_polarity_overrides`: a scan-enable is by definition an
external test pin, and an internal net with the attribute conveys
nothing actionable (issue #44 shipped this half on its own).

**Detection rides the existing walk.** `domain.trace_clock_root` grows
a second observer alongside the #269 `on_combine` recorder:
`ClockControlSink`, invoked from the mux / gate / latch clauses of
`_trace` at the moment the walk resolves *through* one of them, with
the bits of the inputs that steer the selection rather than carry the
clock — a `$mux`'s `S`, and a gate's or latch's non-clock legs.
`domain.find_scan_mode_flops` attaches it to the ordinary per-flop
clock-root walk (same construction as `find_clock_combines`) and
returns the flops whose walk hit a control input satisfying an
**injected** `is_scan_control` predicate. The predicate is injected
because the attribute vocabulary belongs to `rules`, which imports
`domain` and not the reverse;
`rules.scan_mode_clock_select_flops` supplies the real one
(`_backward_fanin` from the control bits to the top-level input ports,
tested against the tagged set, so `!scan_en` and `scan_en | test_mode`
both count). Both recorders observe; neither changes the walk's
outcome. The point is the same as #269's: the fact is read off the walk
that already resolves the clock, so "this flop is clocked through a
scan mux" cannot drift from the tracer's own view of the clock network.

**Tag, don't drop.** `Crossing` gains an additive
`scan_mode: bool = False`, stamped by `find_crossings` from the
`scan_mode_flops` set at all three emission sites (flop-, port-, and
boundary-sourced) — keyed on the **destination**, since the scan
structure is in the destination's clock network. The set never adds,
drops or reshapes a crossing: a run that passes it and a run that does
not produce the same crossings in the same order.

**Suppression is one line, in one place.** `run_all(...,
ignore_scan_mode=True)` filters the shared crossing list before
dispatch. Filtering there rather than per-rule is what makes the
promise checkable — every rule that consumes crossings honours it
identically and none can drift. Default is **off**: a DFT structure
that is genuinely live in mission mode is a real hazard, so the
conservative side is "behave exactly as before the flag existed".

**Two things the flag deliberately does not do.** It does not touch
CDC-010 or CDC-023, which walk the clock network rather than the
crossing list — "don't flag the data paths behind my scan mux" is not
the same claim as "the mux itself is fine". And it does not reach the
compositional per-module pass (§ boundary composition), which runs its
own `run_all` on a greybox body; a scan mux inside an abstracted
module still fires there. Both are conservative gaps, not silent ones.

**Nothing is silent.** `AnalysisResult.scan_mode_suppressed` carries
the tally (counted on the same `async_crossings` list the rule pack is
about to filter, so the two Ns are equal by construction), rendered as
a design-summary line in text and as `summary.scan_mode_suppressed` in
JSON — additive, and `0` without the flag. The per-crossing
`scan_mode` tag is emitted in the JSON crossing record and in the
`--verbose` crossing listing **unconditionally**, flag or no flag, so
a run that suppresses nothing is still auditable.

Fixtures: `good_scan_mode_ignored` (all-scan design: CDC-001 without
the flag, clean with it) and `bad_scan_mode_functional_crossing` (the
same scan mux plus an ordinary functional crossing that must survive
the flag — a tool that cleared that design would be hiding a real bug
behind a DFT annotation).

### 8.21 CDC-026 — dual-clock memory read with no synchronised control (issue #326)

**The hazard.** A word written on clock W and read on an asynchronous
clock R crosses through the storage array. The read data is never
synchronised — in a correct design it does not need to be, because the
reader only reads words it *knows* are stable. That knowledge has to
come from W: in the async FIFO, the gray-coded write pointer crosses
into R through a multi-flop synchroniser, the empty flag compares
against it, and the read pointer — the read address — only advances
while not empty. Without any such path the reader cannot know which
words are stable and can sample one mid-write.

**Model.** `rules.memory_crossings` produces one `MemoryCrossing` per
(memory, read port, read clock R, write clock W ≠ R). The read clock of
a clocked port is its `CLK`; of an asynchronous port, the domain(s) of
the flops its data reaches through combinational logic; and when it
reaches none (a FWFT `assign rdata = mem[raddr]` straight to an output),
the domain its address is computed in — its fanin flops and
SDC-typed input ports. A read port whose domain still does not resolve
gets no record (never a guessed one).

**What counts as sanctioned.** A record's `control` is every crossing
from W into R (the list the rule pack runs on) whose destination flop
lies in the read port's **control cone**: the transitive fanin of its
address + enable, walking through combinational cells and through
R-domain flops' `D` / `EN` (not their resets — a reset synchronised from
W is not a handshake), stopping at flops of any other domain. The
async-FIFO cone reaches the read pointer, the empty flag, the
synchroniser's last stage and then its first stage — the pointer
crossing's destination. **CDC-026 fires when `control` is empty** and
the SDC declares W and R asynchronous; one finding per (memory, R),
anchored on the read port cell, naming the memory and every uncovered
write clock.

**Why not more, and why not less.** The rule proves a structural
dependence of the read side on write-domain information — nothing more:

- It does not judge the control crossing. A binary (non-gray) pointer
  or a single-flop sync is a broken crossing that CDC-004 / CDC-001
  already report on the crossing itself; repeating it as CDC-026 would
  double-report one bug. The domain map's verdict separates the two:
  `synchronizer: "fifo"` only when one control crossing is itself
  recognised by `crossing_synchronizer`.
- It cannot prove the reader never addresses the word being written,
  that the pointers' full/empty arithmetic is right, that two write
  clocks do not collide, or that the writer cannot overwrite unread
  data (no R→W path is required: a read-only-after-configuration table
  has none). Those are protocol properties.
- A memory whose reads are separated from writes by a protocol outside
  the netlist (quasi-static tables, an external handshake) fires and is
  waived by memory name — the same trade CDC-001 makes for a
  `(* cdc_static *)`-less config register.

**Severity `error`.** With no control path inside the analysed design
the hazard is as unambiguous as an unsynchronised flop crossing.

**No silent pass.** The recognised FIFO raises nothing, but it is not
invisible: the domain map lists every memory crossing (`memory_crossings`,
an additive key present only when the design has memory cells) with
its `control` destinations, `synchronizer` (`"fifo"` / `null`) and
`findings`. That is where the recognised-safe flop crossings live too.

**Fixtures.** `bad_dualport_ram_crossing` (registered read, no control
— the former #176 sentinel), `bad_dualport_ram_async_read` (async read
through logic into an R register), `good_async_fifo_inferred_ram`
(gray pointers + 2FF syncs; registered and FWFT read ports, both
recognised), `good_same_clock_ram` (silent), and
`bad_ram_write_addr_wrong_domain` (CDC-004 on the write port, no
CDC-026). Both frontends reach the same verdict on all five.

### 8.21 CDC-024 — top-level output launched asynchronously to its contract (issue #323)

`set_output_delay -clock C [get_ports o]` declares that whatever sits
outside the design captures `o` on `C`. `_handle_set_delay` has always
recorded that in `ClockSpec.port_clock`, but the only consumer was the
*input* port walk in `find_crossings`, and crossings only terminate on
flop `D` pins. So an output launched from a domain the SDC declares
asynchronous to `C` produced no crossing, no finding and no
diagnostic — a declared contract the run silently never checked.

**Rule, not crossing.** Modelling the external capture as a synthetic
sink flop (the §7.4 boundary-sink trick) was rejected: every
crossing-consuming rule would then see it. CDC-001 would walk a
"sync chain" behind a pin, CDC-004 would bus-check it, CDC-005 would
look for reconvergence — none of which means anything for a capture
the netlist does not contain, and each would need a special case to
stay quiet. CDC-024 is a standalone walk over the contract instead,
like CDC-021 / CDC-023: `find_crossings`, `summary.crossings` and
every existing rule are untouched.

**The walk.** For each top-level port with direction `output` and a
real (non-sentinel) `port_clock` entry:

1. `_output_cone` walks the output's bits backward through
   combinational cells to the `Q` pins that launch it and to any
   top-level input ports it reaches.
2. Each launching register's domain comes from
   `domain.flop_domain_resolver` — the same function `assign_domains`
   uses, built lazily (only when some output cone reaches a register)
   and memoised per flop. A typed input port in the cone launches in
   its `set_input_delay` clock. A launcher whose domain does not
   resolve is skipped (it is already counted in
   `summary.domain_unknown`); so are latches and untyped
   (`<unconstrained>`) inputs.
3. A launch domain `L` is asynchronous to the contract `C` by the
   exact predicate `filter_async` applies to a crossing: both names
   normalised through `clock_for_port`, then not
   `is_unreachable_crossing(L, C)` and `are_async(L, C)`. So a
   *related* contract — `C == L`, a generated clock of `L`, a virtual
   clock never placed in an async group — is silent no matter how the
   names differ, and an explicit `set_clock_groups -asynchronous`
   naming a generated clock still wins over the master collapse.
4. If any launcher is asynchronous, `_reached_output_bits` replays
   the cone forward from those launchers only (lane-aware, as in the
   crossing walk) to count the output bits they reach.

One finding per output port, `cell_name` anchored on the cell driving
the lowest affected output bit — the launching register for a
registered output, the last gate for a combinational one — so the
source location (and an in-RTL pragma's scope) is where the output
gets its value. The message names the port, the affected bit count for
a bus, each asynchronous launch domain with a sample of its launchers,
and the declared receiving clock. Outputs with no `set_output_delay`
are CDC-025's (§8.22).

**Severity `warning`.** An asynchronous output is correct engineering
whenever the receiving design synchronises it, and that receiver is
exactly what this analysis cannot see — the textbook review-or-waive
case of §8.2, not an unambiguous bug.

**Intentional asynchronous outputs.** No new mechanism: an ordinary
waiver — `waive CDC-024 <port-regex> <reason>` in the waiver file, or
`// rbcdc: disable-rule CDC-024 <reason>` around the driving logic on
the `lint` path. A waiver does not drop the finding; it moves it to
`suppressed`, where JSON keeps the full message plus the reason and
SARIF keeps the result with a `suppressions` entry. That *is* the
explicit, reviewable record of the external synchronisation
obligation the issue asks for. `set_false_path -to [get_ports o]` was
considered and rejected: it is path-specific STA syntax the parser
does not interpret today (§6.3 partial warning), and honouring it
would make the output silently unchecked rather than visibly waived.

**Scope limits.** Cone depth is 12 combinational cells (the shared
cone-walk default). Ports named by bit-select or wildcard in the SDC
(`[get_ports {o[3]}]`, `o_*`) are not resolved — the same limit the
input-port walk has. An output driven from an abstracted blackbox
boundary pin has no `Q` in the parent's cone and is not checked. The
compositional per-module pass drops port typings
(`derive_sub_spec`), so CDC-024 is inert there by construction.

CDC-024 is a **new rule id** under the CDC-022 / CDC-023 discipline:
ids are added, never renamed or reused. Fixtures:
`bad_output_async_contract` (direct, combinational, vector and
half-asynchronous vector outputs, two contracted to a virtual clock;
fires once per port and nothing else) and `good_output_sync_contract`
(same-clock, virtual-related, forwarded-generated and second-clock
contracts; silent). `tests/test_cdc_024_output_boundary.py` covers the
issue's reproducer and the corners on hand-built netlists, under both
SDC reader backends.

### 8.22 CDC-025 — top-level output with no `set_output_delay` contract (issue #329)

CDC-024 can only check an output that has a contract. An output with
no `set_output_delay -clock` was skipped silently — the output-side
mirror of what CDC-011 covers for untyped primary inputs. CDC-025
reports those outputs when clocked logic drives them, naming the port
and its launch domain(s).

**Same walk as CDC-024, shared.** Cone, launchers, domains and the
async predicate are CDC-024's: `_output_cone`, `flop_domain_resolver`
(memoised per flop), `canonical` + `is_unreachable_crossing` /
`are_async`. Both rules read them through `_output_launch_index`, a
per-run memo on the rule context, so an output is walked at most once
whichever rules run — CDC-024 walks the contracted outputs, CDC-025 the
rest, and a test pins that no output is walked twice.

**Two shapes, CDC-011's split.**

- **`error`, always on — asynchronous merge.** Some *bit* of the output
  is a combinational function of launches from two domains the SDC
  declares mutually asynchronous (`irq = irq_a | irq_b`; a `clk_b`
  enable gating a `clk_a` bus). A boundary signal mixing asynchronous
  domains has no single clock any receiver could capture it on and can
  glitch whenever either side switches; no `set_output_delay` makes it
  right, which is why this half ignores the SDC's output typing
  entirely. The merge is judged **per bit**, not per port: for each
  launch domain, `_reached_output_bits` replays the cone forward from
  that domain's launchers, and a bit reached from both sides of an
  asynchronous pair is a merge. A port-level test would escalate every
  status bus that concatenates fields from two domains (`{st_b, st_a}`)
  — common, and correct — so the per-bit reading is the one that
  matches the issue's "combinational merge". The finding counts the
  merged bits and anchors on the cell driving the lowest of them.
- **`warning`, self-calibrating — missing contract.** Every bit is
  launched from one domain (or from related domains: a generated clock
  and its master), or a bus's domains never meet on a bit. This is a
  constraints-completeness gap, not a bug, so it fires **only when the
  SDC types at least one top-level output of the module** with
  `set_output_delay -clock`. Rationale: every existing design has
  untyped outputs, and a default-on warning would add a finding to
  nearly every report on upgrade (measured when the rule landed: 148 of
  the 166 pre-existing fixture netlists, 189 findings, including 46 of
  the 48 `good_*` fixtures — and each of the three project-template CDC
  demos elaborated with yosys); an opt-in flag would add CLI
  surface for a check most users would never turn on. A user who types
  some outputs has started declaring capture contracts, so the ones
  left out are likely omissions — the gate calibrates itself from the
  SDC with no new flag. The `single_domain` keyword on `check_cdc_025`
  overrides the gate for tests and library callers (`True` always,
  `False` never); the CLI does not expose it. A mixed-domain bus that
  no single contract can describe gets a "split the port per domain"
  hint instead of a `set_output_delay` suggestion (the parser does not
  resolve bit-selected ports, §8.21 scope limits).

One finding per output port; a bus is one finding.

**Exclusions.**

- Contracted outputs — CDC-024's, so an output **with** a contract
  never gets CDC-025, even when it merges asynchronous domains (CDC-024
  then reports the launch asynchronous to the contract; a merge whose
  every launch is related to the contract clock — a virtual clock in no
  async group — stays silent, as CDC-024 always has).
- Clock outputs: a port the SDC declares a clock on (`create_clock` /
  `create_generated_clock` targeting it), an output whose cone reaches a
  declared clock input port (forwarded or gated clocks), and an output
  whose net is a flop `CLK` or is driven by a `_clock_network_cells`
  cell (e.g. a divider register that also clocks internal flops). A
  divider whose only load is the output is indistinguishable from data;
  declare it with `create_generated_clock [get_ports …]` to exclude it.
  The clock-network set is computed lazily, only for an output that
  otherwise qualifies.
- Outputs with no recognised register launching them: constant or
  undriven bits, a register whose `D` is all constant (it never
  launches data; yosys's `opt` folds it away while slang keeps it, so
  counting it split the two frontends on the grammar fuzz oracle), a
  latch or blackbox-pin driver, and **pure
  combinational feed-throughs of input ports**. A feed-through is
  launched outside the design; the input's own typing (CDC-011) is the
  place to declare its domain. A typed input that *does* meet a
  register in the cone counts as a launcher in its `set_input_delay`
  clock, exactly as CDC-024 counts it — so a `clk_b`-typed input gating
  a `clk_a` register onto an output is a merge.
- Launchers whose domain did not resolve (already in
  `summary.domain_unknown`) or resolved to the `<unconstrained>`
  sentinel of an undeclared clock port (CDC-021's finding). Note this
  makes CDC-025 sensitive to resolution: raising `--clock-trace-depth`
  or teaching the tracer a new clock-path shape can reveal a merge that
  was hidden behind an unresolved flop (`deep_clock_divider_chain`,
  `clock_through_latch`). That is the rule working, not a regression;
  the parity tests that compare resolution modes exclude it explicitly.

**Intentional cases.** No new mechanism, as for CDC-024: `waive
CDC-025 <port> <reason>` or `// rbcdc: disable-rule CDC-025` on the
driving logic; the finding moves to `suppressed` with its message.

**Compositional pass.** Excluded (`INTERNAL_RULE_EXCLUSIONS`, §4.9): a
subtree output is a hierarchy pin, not a top-level output. The
single-domain half is inert there anyway (`derive_sub_spec` drops port
typings, so the gate is closed); the merge half would otherwise fire on
an internal net the parent's crossing walk already covers.

**Existing reports that change.** With default flags, the merge half
fires on six existing fixtures, all of them the "parity anchor" outputs
that XOR a `clk_a` register with a `clk_b` one to keep logic alive
(`bad_unresolved_clock_latch`, `clock_through_latch`,
`declared_pin_clock`, `inferred_fwd_clock`, `scaling_perf` flat and
hierarchical, `bbx_residual_declines` flat) — true positives of the
exact shape the rule targets. No `good_*` fixture changes; the
single-domain half fires on no pre-existing fixture (the only two that
type outputs, CDC-024's pair, type all of them).

CDC-025 is a **new rule id** under the CDC-022 / CDC-023 / CDC-024
discipline. Fixtures: `bad_output_async_merge` (an OR merge and a
mux-gated bus merge fire as errors; a concatenated mixed-domain bus and
a single-domain register stay silent with no output typed) /
`good_output_async_merge_synced` (each foreign source 2FF-synchronised
first), and `bad_output_missing_contract` (one output typed; three
uncontracted data outputs warn; every exclusion present and silent) /
`good_output_contracts_complete`. `tests/test_cdc_025_unconstrained_outputs.py`
covers each shape, exclusion and the CDC-024 interplay on hand-built
netlists under both SDC reader backends.

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
and waiver-line number), not silently dropped. A third producer shares
the channel without going through `apply`: an object-scoped
`set_false_path` acknowledges findings on the crossings it covers, with
the SDC statement as the reason (§6.4, `Waiver.sdc`). They appear in JSON
output and in SARIF as `suppressions`. They don't drive the exit
code, so a fully-waived run returns 0.

This deliberately mimics the common `.swl` waiver-file workflow at a much
smaller surface — one scope qualifier (below), no severity overrides, no
expiry dates. Add those when a real project asks for them, not
preemptively.

**Instance scope (#327).** An `inst:<regex>` word, as the third word
(instance-only) or the fourth (after the object regex), sets
`Waiver.inst_regex`. `waivers.apply` `re.search`es it against the
violation's instance scope — by default `"/".join(instance_path)`,
`""` at top; the CLI passes an `instance_scope` resolver that appends
the blackbox instance itself when `cell_name` names a blackbox cell
(`CDC-BBX`, greybox-lifted findings). Both patterns must match. The
CLI resolves `instance_path` *before* applying waivers so the scope is
always populated. JSON `suppressed[].waiver` gains `inst_regex`
(pattern or null) — an added key, like `origin` / `end_line`.

### 9.1 In-RTL pragmas (`pragma.py`)

A second producer feeds the same `Waiver` list: a magic comment in the
source, in this tool's `rbcdc:` namespace (org-wide convention — one
prefix per rtl-buddy tool; there is no SV-attribute form).

```systemverilog
// rbcdc: disable-rule CDC-001,CDC-002  hand-reviewed handshake
/* rbcdc: disable-rule CDC-005 library cell */
```

An `enable-rule` closes the region a `disable-rule` opened, Spyglass
style:

```systemverilog
// rbcdc: disable-rule CDC-001 vetted by hand
always_ff @(posedge dst_clk) q <= src_q;
// rbcdc: enable-rule CDC-001
```

`pragma.scan(sources)` reads the files as **text**. It never goes
through Yosys or slang: the scan is frontend-independent, costs
nothing, and is unaffected by an elaboration failure. Each
(region × rule id) becomes one `Waiver` whose `regex` is the file's
escaped basename, whose `source_line` / `start_line` is the
`disable-rule` line, whose `end_line` is the closing `enable-rule` line
(`None` = to the end of the file), and whose `origin` holds the source
path.

Pairing is per rule id, so interleaved blocks for different rules are
independent. Re-`disable`-ing an open rule ends the running region and
starts a fresh one; a stray `enable-rule` is ignored.

`origin` is what distinguishes the two producers, in both matching and
reporting:

- **Matching.** `waivers.apply` takes a `locate` resolver returning a
  `SourceRef(file, line)` (the CLI supplies one — it needs the
  `Module`, since the location lives on the offending cell's `src`
  attribute). A waiver with an `origin` is matched against that
  location *only*: file first, then `[start_line, end_line)`. Its
  scope is a region of a file, not a name pattern, so it is never
  tried against `cell_name` or `message`. A violation with no
  resolvable location is never waived by a pragma; one located to a
  file but not a line falls back to file scope, because a range check
  it cannot perform should not silently swallow the suppression.
- **Reporting.** The text report prints `(pragma <file>:<line>)` — or
  `(pragma <file>:<start>-<end>)` for a closed block, which is what
  makes a block-scoped waiver visibly narrower than a file-scoped one
  — instead of `(waiver line N)`. The JSON `suppressed[].waiver`
  object carries `origin` (null for a waiver-file entry) and
  `end_line` alongside `source_line`. Both are added keys — a consumer
  reading the pre-existing fields is unaffected.

Only `lint` scans: `analyze` starts from an elaborated netlist and has
no sources. Pragmas are prepended to the `--waivers` list, so
first-match-wins gives an inline suppression precedence over a broad
file regex.

### 9.2 Baseline filtering (`--baseline`)

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
pack). It is its own contract: `schema_version` (currently `"1.3"`) is
pinned by `domain_map.SCHEMA_VERSION`, breaking changes require a bump, and the
artifact ships sorted deterministically so consumers can golden-diff
against the same inputs. `--no-findings` short-circuits rule
evaluation when the map is the sole deliverable. Primary consumer
today is [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view).

Schema 1.3 also carries, additively, the object-scoped false-path
records (`false_path_exceptions`) and the per-crossing
`sdc_exceptions` / `sdc_acknowledged` annotation (#328, §6.4).

Schema 1.3 (#325) adds, additively, the SDC constraints that carry CDC
intent without declaring a clock: `datapath_only_pairs` (the clock
pairs a `set_max_delay -datapath_only` marks async — sorted pairs, the
same shape as `false_path_pairs`) and `max_delay_datapath_only` (every
datapath-only record: `from_clocks`, `to_clocks`, `from_objects` /
`to_objects` as `{kind, name}` exactly as written, `value`, and
`clock_pair` saying whether it contributed a pair; object endpoints are
resolved since #328 and the crossings they cover carry `sdc_exceptions`),
plus `case_analysis`
(`{port, value}` per `set_case_analysis` target, sorted by port — the
`flop_domains` already reflect any clock mux it resolved). A consumer can
match a crossing's `src_clock` / `dst_clock` against
`datapath_only_pairs` to see which crossings are constrained.

Schema 1.2 (#307) adds a per-crossing verdict to every `crossings[]`
and `clock_network_crossings[]` entry, so a consumer can tell a
recognised synchroniser from a raw path without re-running the rules:

- `synchronizer` — the scheme the rule pack recognises at the
  destination, or `null`. Vocabulary (`rules.SYNCHRONIZER_SCHEMES`,
  additive only): `"2ff"` (multi-flop chain of depth ≥ 2, or a
  `(* cdc_sync *)` destination), `"pulse"` (that chain fed by a toggle
  source with the XOR-tail recovery CDC-013 accepts), `"gray"`
  (`(* cdc_gray *)` source, or a structurally gray-encoded source into a
  lane-aligned multi-bit chain or a gated load), `"handshake"` (gated
  load whose source holds behind a synced-back ack, or a
  `(* cdc_handshake *)` endpoint), `"mux"` (gated load without that
  ack — CDC-012 decides whether that is a finding), `"static"`
  (`(* cdc_static *)` source) and, on clock-network crossings only,
  `"glitchless"` (`(* glitchless_clock_mux *)` select). Computed by
  `rules.crossing_synchronizer` / `rules.clock_network_synchronizer`
  from the same predicates the rules use for their exemptions, against
  the same `_RuleContext` the rule pass ran with — no separate
  recognition logic.
- `findings` — sorted, de-duplicated rule ids raised on that crossing
  after waivers / pragmas (baseline carry-over included); `[]` is
  "checked, clean", `null` is "the rule pass did not run" (no SDC, or
  `--no-findings`). Matching uses the identity the finding already
  carries — the exact `Crossing` for crossing-carrying rules, the
  fan-out group for CDC-005, the port for CDC-011, the named cell
  otherwise; see `domain_map._FindingIndex`.

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
  consult it in the rules that should honor the annotation. For an
  attribute that declares something about an external *pin* rather than
  a producing flop, follow `user_reset_polarity_overrides` /
  `scan_mode_port_names` and filter to top-level ports instead.
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
- **New facts about the clock network.** Add an observer to
  `trace_clock_root` rather than a second traversal: `CombineSink`
  (#269, §8.19) and `ClockControlSink` (#45, §8.20) are the pattern.
  Record at the site the walk already visits, keep the callback purely
  observational, and build the consumer on top of the recorded events —
  that is what stops a derived fact from drifting away from the
  tracer's own behaviour.
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

- `find_crossings` is `O(F · max_hops · avg_fanout)` in the worst
  case, where F is flop count. A backward pass per clock domain
  (`_make_fanout_filter`) first finds which bits can reach a
  foreign-domain flop within the hop budget; the forward walk expands
  only those, so same-domain logic costs one check per source bit.
- CDC-004 / CDC-020 / the domain-map verdict find a multi-bit
  synchroniser's next stage through `_RuleContext.flops_by_d`, an index
  of flops by their `D` bit tuple, rather than rescanning every cell
  per crossing.
- CDC-012's handshake-feedback walk caches each register's input
  fanin per run, and the backward / forward cone walks
  (`_backward_flop_fanin`, `_forward_reachable_cells`) expand each
  combinational cell at most once. Both walks go level by level, so a
  cell's first expansion is at its shallowest depth and later ones
  can reach nothing new; without this a W-bit cell cost O(W²).
- `trace_clock_root` is called once per flop. `_bit_drivers` is
  precomputed once and shared. The same holds at blackbox boundaries:
  `compose_boundaries` and the CLI's CDC-008 clock-pin pass each build
  the parent's driver map once and pass it as `drivers=` to
  `_instance_clocks` and its callers. Rebuilding it per instance was
  O(cells × instances) (#320).
- CDC-024 costs nothing on a design with no `set_output_delay`. With
  contracts it does one expanded-once backward walk per contracted
  output (`O(cone)`), traces the clock of each launching register once
  (memoised), and replays the cone forward only for outputs that fire.
- CDC-025 costs nothing on an SDC with no asynchronous relation while
  no output is typed (the only live shape needs one). Otherwise it does
  one backward walk per *uncontracted* output — shared with CDC-024
  through `_output_launch_index`, so no output is walked twice — reuses
  the same memoised launch-domain resolver, and replays the cone forward
  once per launch domain for an output with an asynchronous pair among
  its launchers (once in all for a reported single-domain output). `_clock_network_cells` (one `O(design)` pass) is
  built lazily, for the first output that otherwise qualifies.


- The reset-side views are `_RuleContext` cached properties built at
  most once per run (#324): `reset_domains`
  (`assign_reset_domains`), `reset_synchronizers`
  (`find_reset_synchronizers` plus the `reset_sync` marks) and
  `reset_sync_chains` (`iter_reset_sync_chains`), all from the
  context's own flop list and driver map. RDC-002 / -004 / -005 /
  -006 / -007 / -008 read them instead of rebuilding them, and
  CDC-008 / CDC-010 share `clock_network_cells` the same way. The
  public `reset_domain` helpers take the precomputed pieces as
  optional keyword arguments and build them when omitted.
- `Module.port_of_bit` is a dict lookup: the bit → port index is a
  `cached_property` built on first use (first port in `ports` order
  wins, as the old scan did). It is not a dataclass field, so `==`,
  `hash` and `repr` are unchanged (#324).
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
