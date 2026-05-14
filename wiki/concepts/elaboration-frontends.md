---
title: Elaboration Frontends
created: 2026-05-14
updated: 2026-05-14
type: concept
tags: [frontend, elaboration, yosys, slang, pyslang, architecture]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# Elaboration Frontends

`netlist.Module` is the contract every rule walks; **how a module gets built is pluggable**. `rtl-buddy-cdc` ships two frontends behind a single factory, selected by the `--frontend {yosys,slang}` CLI flag.

## The Contract

Every frontend must produce a `Module` shape the rule pack can consume unchanged:

- **Yosys-style cell types** — `$dff`, `$adff`, `$and`, `$or`, `$xor`, `$mux`, `$logic_and`, …
- **Pin names** — `CLK` / `D` / `Q` / `ARST` on flops, `A` / `B` / `Y` (and `S` on muxes) on combinational cells
- **Integer bit IDs** for nets, with the four constant chars `"0"` / `"1"` / `"x"` / `"z"` reserved
- **SV `(* attr *)`** declarations propagated onto the corresponding `Netname` so attribute-driven rules (`cdc_sync`, `cdc_gray`) work uniformly

Rules don't know which frontend produced a `Module` — the shape is the only coupling. New frontends only need to produce that shape; the rule pack, SDC parser, waiver matcher, and reporter all stay unchanged.

## The Factory

`frontend.elaborate(sources, top, frontend=Frontend.yosys, **kw) -> Module` is the single orchestration entry. It dispatches on the enum to the concrete frontend module.

```python
from rtl_buddy_cdc.frontend import Frontend, elaborate

module = elaborate(["rtl/foo.sv"], "foo_top", frontend=Frontend.slang)
```

The `analyze` CLI command bypasses the factory — it takes a pre-elaborated Yosys JSON via `netlist.load` directly. `lint` is the entry that drives a frontend.

## Yosys Frontend (`frontends/yosys.py`)

Runs `yosys -p 'read_verilog ...; hierarchy -top X; proc; flatten; opt_clean; write_json /tmp/out.json'`, then loads the JSON via `netlist.load`. This is the historical primary path and remains the default for `lint --frontend yosys` and the `analyze --netlist file.json` entry. Requires a `yosys` binary on `PATH` (override with `--yosys PATH`).

## slang Frontend (`frontends/slang.py`)

Elaborates SV sources directly via the [pyslang](https://pypi.org/project/pyslang/) binding to [slang](https://github.com/MikePopoloski/slang) and builds a `Module` from the elaborated `pyslang.Compilation` — no synthesis subprocess, no `flatten` step.

Opt-in via the `[slang]` install extra (`pip install 'rtl-buddy-cdc[slang]'`); the default install stays `typer`-only. When pyslang is missing, the frontend raises `SlangFrontendUnavailable` with an install hint. Fatal pyslang diagnostics surface through a `TextDiagnosticClient` with file:line:col + caret summaries — the usual compiler-error UX.

### Internals

The translator (`_ModuleBuilder`) walks the elaborated top instance in three passes:

1. **Variables** — local `logic` / `reg` become `Netname`s with hierarchical name prefixes (`u_b0.q`); attributes pulled via `Compilation.getAttributes(symbol)` (pyslang stores them on the compilation, not the symbol).
2. **Ports** — top-instance ports only; child ports get folded into their parents' connection expressions.
3. **Cells + continuous assigns + child instances** — the lowering layer.

Procedural and expression shapes lower to Yosys-shape cells:

- `always_ff` with the canonical async-reset shape → `$dff` or `$adff` with `CLK` / `D` / `Q` / `ARST` connections; reset polarity comes from the event edge confirmed against the conditional body.
- `BinaryExpression` / `UnaryExpression` / `ConditionalExpression` → `$and` / `$or` / `$xor` / `$mux` / `$not` / `$reduce_*` / etc. with the Yosys A/B/Y/S pin convention.
- `always_comb` blocks alias their LHS variables to the lowered RHS bits.
- `ElementSelectExpression` / `RangeSelectExpression` on either side → bit subset of the underlying variable.
- `ConcatenationExpression` (`{a, b, c}`) and `ReplicationExpression` (`{N{x}}`) on the RHS — pure bit-tuple aliasing in LSB-first order, no cell emitted (matches Yosys post-`opt_clean`). Constant replication counts only.

### Source Locations

Every emitted cell carries `attributes["src"]` formatted as `"file:line.col-line.col"` — the same convention Yosys writes after `flatten`. The helper resolves a usable range in priority order: the node's `syntax.sourceRange` (best — spans the whole `always_ff` block for `ProceduralBlockSymbol`, which has no useful `.sourceRange` directly), then `node.sourceRange` (Expression-level nodes have this), then a degenerate range from `node.location`. Returns `None` and skips the attribute when nothing usable is available — matches Yosys' behaviour for sourceless transformations. The JSON / SARIF reporters surface these as clickable file:line locations without a frontend-specific branch.

### Cross-Module Flattening

Child `InstanceSymbol`s are walked recursively. For each port connection the child's internal-port variable is aliased to the parent's expression bits — so flop A's `Q` in one instance and flop B's `D` in the next resolve to the same net. Hierarchical cell + netname prefixes (`u_b0.q`, `u_b0.$slang$adff$3`) match Yosys-flatten output. Aliasing rewrites propagate globally across `_var_bits` / `_ports` / `_netnames` so chains like parent `a_q` ← child `q` collapse to a single net rather than leaving stale aliases.

### Parity Status

The slang frontend reaches **parity with the Yosys frontend on every SDC-equipped fixture in the regression suite** — CDC-001 through CDC-008, attribute suppression (`cdc_sync` / `cdc_gray`), multi-bit LHS bit-selects, and multi-module hierarchies. The per-fixture matrix lives in the `frontends/slang.py` module docstring; the slang-specific tests live in `tests/test_slang_elaboration.py` (rule parity) and `tests/test_slang_lowering.py` (operator coverage).

Two fixtures stay Yosys-frontend-only by design: `bad_source_sync_internal` / `good_source_sync_internal` embed Yosys-internal `$_BUF_` primitives in their SV source — slang correctly rejects them as not legal SystemVerilog.

## When To Use Which

| Goal | Frontend |
|---|---|
| Default workflow, has `yosys` installed | Yosys (the default) |
| Want to skip the synth step, avoid the Yosys runtime dependency, or work with SV constructs Yosys mangles after flatten | slang |
| Already have a pre-synthesized Yosys JSON (e.g. from `rb synth`) | Neither — use `analyze --netlist file.json` directly |

Both frontends produce the same violation set on the standard fixture suite, so swapping between them is safe for CI / regression work.

## Adding A New Frontend

The factory pattern makes it a localised change:

1. Add a submodule under `frontends/` exposing `elaborate(sources, top, **kw) -> Module`.
2. Register the new name in the `Frontend` enum in `frontend.py`.
3. Dispatch to it in `frontend.elaborate`.
4. The frontend must produce the Yosys-style `Module` contract documented above; the rule pack consumes that contract regardless of source.

No changes needed in `rules.py`, `sdc.py`, `waivers.py`, or `reporter.py`.

## Related Pages

- [[rtl-buddy-cdc]] — the tool entry that consumes a frontend's output
- [[cdc-analysis-pipeline]] — the downstream pipeline that runs on the produced `Module`
- [[cdc-data-model]] — the `Module` / `Cell` / `Port` / `Netname` contract every frontend produces
- [[cdc-testing-strategy]] — paired bad/good fixtures that gate frontend parity
