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
- Not a layered hierarchical analyzer. Today the netlist must be
  fully flattened. Hierarchical reporting is on the roadmap (see
  [`README.md`](../README.md)) but not architecturally present.

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
| `netlist.py` | Parse Yosys `write_json` output into typed structs | `Module`, `Cell`, `Port`, `Netname` |
| `flops.py` | Recognise the 11 Yosys FF cell variants and extract CLK / D / Q | `Flop`, `FF_CELL_TYPES` |
| `domain.py` | Trace clock roots, find flop→flop crossings | `FlopDomain`, `Crossing`, `trace_clock_root`, `find_crossings` |
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
3. **Source endpoint is either a flop or a port.** Exactly one of
   `src_flop` and `src_port` is set. Port endpoints are emitted by
   `find_crossings(module, port_clock=...)` when the SDC has typed
   the port via `set_input_delay -clock <c>`. The convenience
   property `src_name` returns either the flop name or `"port <p>"`
   for messages and waiver matching.

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
    rule_id: str           # "CDC-001" .. "CDC-008"
    severity: str          # "error" | "warning" | "info"
    message: str           # human-readable
    crossing: Crossing | None    # the offending crossing (most rules)
    cell_name: str | None        # most-responsible cell (for src locations)
```

`crossing` is `None` for rules that operate on a non-data shape
(CDC-007 reset crossings, CDC-008 clock-as-data). `cell_name` lets
structured reporters surface a `file:line:col` source location by
looking at `cell.attributes["src"]`.

### 4.7 The reporter contract

`AnalysisResult` is the immutable struct that flows into every
formatter. It contains everything any output mode might need:
`module`, `domains`, `crossings` (all of them), `async_crossings`
(after SDC filter), `spec`, `violations` (kept), `suppressed`. No
formatter does additional analysis — this struct is the boundary
between "analyzer" and "presentation".

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

The walker maintains a per-call `seen` set keyed on bit ID and a
`max_depth` counter (default 16), so cycles in the clock network
terminate cleanly. The depth budget is intentionally low — clock
networks rarely exceed a handful of hops, and a deep walk is more
likely to be following data than clock.

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
hand-rolled `shlex` tokenizer is the right tool: real Tcl
interpreters (`tkinter.Tcl()`) execute user code, complicate
deployment, and add a non-Python dependency to the wheel.

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

Plus: `#` comments, `\` line continuation, and a permissive
flag-skipping pattern for unrecognised options on otherwise-known
commands (so vendor-specific dialects don't choke).

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

The `-source` argument is consumed by scanning forward to the next
`-` flag rather than skipping a fixed token count, because shlex
splits `[get_ports ck_a]` into two tokens (`[get_ports` and `ck_a]`).
A fixed skip would leak the trailing-`]` half into the target list
and silently mis-attribute the generated clock to whichever name fell
out of the bracket parsing.

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
hint).

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

## 8. The rule pack

`rules.py` is a flat collection of `check_<rule>` functions plus a
small registry:

```python
RULES: dict[str, RuleFn] = {
    "CDC-001": check_cdc_001,
    "CDC-002": check_cdc_002,
    ...
    "CDC-008": check_cdc_008,
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
| `_is_gated_bus_crossing(...)` | CDC-004 | Recognise handshake-style gating — D updates only when an enable from the dst domain has been synchronized across |
| `_clock_network_cells(...)` | CDC-008 | Identify cells whose output transitively drives any flop CLK; they're exempted from "clock as data" |
| `user_sync_flop_names(module)` | CDC-001..-003, -006 | Return cell names of flops the user has annotated `(* cdc_sync *)` |
| `user_gray_flop_names(module)` | CDC-004 | Return cell names of source-side flops the user has annotated `(* cdc_gray *)` |

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
  crossing list, then the violations grouped by severity. Designed
  for terminal and CI-log review.
- **`render_json`** — full structured output. Stable schema. Used by
  `rb cdc` (rtl-buddy's wrapper) to extract violation counts, and by
  any custom dashboard.
- **`render_sarif`** — SARIF 2.1.0, GitHub-Code-Scanning-compatible.
  Populates `tool.driver.rules` for every rule that fired in the run.
  Each result carries `physicalLocation.region` parsed from the
  cell's `attributes["src"]`. Suppressed findings are emitted with a
  `suppressions` field so the alert exists but doesn't fail the
  build.

Format selection is purely a CLI flag (`--format text|json|sarif`);
the analyzer pipeline runs the same way regardless.

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
