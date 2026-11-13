---
title: SDC Parsing
created: 2026-05-11
updated: 2026-11-13
type: concept
tags: [sdc, eda, parsing, clock-domain, design-decision]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# SDC Parsing

SDC is Tcl syntactically. `sdc.py` reads it with one of **two interchangeable Layer-1 backends** (rtl-buddy-cdc#298); both feed the same per-command `ARG_SPECS` slicer, handlers and `ClockSpec`, so every downstream consumer is backend-agnostic.

| Backend | Selected when | Evaluates |
|---|---|---|
| `tcl` | the worker probe succeeds — `_tkinter` imports in a child (every uv-managed / python-build-standalone interpreter) | `tkinter.Tcl()` → `interp create -safe` → an `unknown` handler aliased back into Python, **in a subprocess**. `set` variables, `expr`, `[…]` command substitution, nested collections (`[get_pins [get_cells u_div]/C]` → `u_div/C`). |
| `tokenizer` | `_tkinter` missing (Homebrew python without `python-tk`, EL8 system python) | `tcl_tokenizer._tokenize` — a word splitter only. `{...}` and `[...]` are single opaque tokens; `$var` / `expr` / substitution are **not** evaluated. |

`sdc.backend()` reports the choice (printed by `rtl-buddy-cdc version`); `RB_CDC_SDC_BACKEND` or `parse(text, backend=...)` overrides it.

**The Tcl backend is a safe interpreter.** `interp create -safe` gives a child with no `exec`, `open`, `file`, `socket`, `load` or `source`, so an untrusted constraints file can neither spawn a process nor touch the filesystem. Those names fall through to `unknown` like any other undefined command and are reported as unsupported — `source` explicitly so, since it cannot pull in a second file.

## The Tcl interp runs out of process — and must stay there

`sdc.py` does **not** import `_tkinter`. The interp lives in `src/rtl_buddy_cdc/tcl_worker.py`, spawned per SDC file as `python -m rtl_buddy_cdc.tcl_worker`, with one JSON request on stdin (`{"text", "command_limit", "time_limit_seconds"}`) and one JSON response on stdout (`{"ok": true, "patchlevel", "commands", "partial_warnings"}`, or `{"ok": false, "kind": "tcl_error" | "resource_limit" | "unavailable", "error"}`).

This is a correctness requirement, not tidiness (rtl-buddy-cdc#298):

- Loading `_tkinter` starts Tcl's `NotifierThreadProc`, a native thread that sits in `select()` for the life of the process and is never joined — there is no API to retire it.
- A `subprocess.Popen` from a process carrying that thread is `fork()` then `exec()`. Between the two the child runs single-threaded in an address space forked from a multi-threaded parent, where only async-signal-safe calls are legal. CPython's `child_exec` calls `_close_open_fds_maybe_unsafe` there, and on macOS a `close()` in that window can enter uninterruptible kernel state and never return. The child never reaches `exec`, never writes the errpipe, and the parent blocks forever reading it.
- Reproduced in the sibling rtl-buddy repo (identical reader code) in 1 of 6 full pytest runs on uv-managed CPython 3.12 with Tcl 9.0.3; the orphaned child sat wedged 9+ hours.

rb-cdc shells out to yosys and the slang frontend *after* it parses the SDC, so an in-process interp puts a permanent hang hazard on the main analysis path. The cost of the fix is one short-lived subprocess per SDC file. A test asserts `_tkinter not in sys.modules` after a tcl-backend parse; another asserts the worker module has no top-level `tkinter` import (it is imported lazily inside `main()`, and `sdc.py` imports the worker module for `WORKER_MODULE`).

Because availability can no longer be decided with `import _tkinter`, `sdc.tcl_available()` / `sdc.tcl_patchlevel()` run the worker once per process with an empty request and cache the answer (`sdc._TCL_PROBE`). The outer `subprocess.run` deadline is `TCL_TIME_LIMIT_SECONDS + TCL_WORKER_TIMEOUT_MARGIN_SECONDS`; blowing it kills the worker and is reported exactly like an `interp limit` overrun. A worker that exits non-zero or writes non-JSON is a `TclReadError`, i.e. a tokenizer re-read with a `partial_warnings` entry.

`-safe` bounds *capability*, not *cost*, so the child also carries `interp limit` budgets: `TCL_COMMAND_LIMIT` (1,000,000 commands, `-granularity 1`) and `TCL_TIME_LIMIT_SECONDS` (30s wall-clock, expressed as an absolute epoch second per `interp limit`'s contract). The command counter catches runaway recursion and million-iteration loops; the wall-clock deadline catches a bare `while 1 {}`, whose empty body dispatches no commands and so never ticks the counter. Either overrun raises `TclResourceLimitError` in the parent, which `parse` handles exactly like any other read error — tokenizer re-read plus a `partial_warnings` entry naming the cut-off.

The original #144 decision was the opposite one ("not a Tcl interpreter"); it was revisited in #298 because rtl-buddy's own SDC reader (rtl-buddy#641) grew the same two-tier shape, and a clock declared through a variable must not be seen by one tool and missed by the other. `tcl_tokenizer.py` is stdlib-only and package-import-free precisely so rtl-buddy can vendor it verbatim as its own fallback.

## Supported Commands

```
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

Plus: `#` comments, `\` line continuation, and permissive flag-skipping for unrecognised options on otherwise-known commands (vendor-specific dialects don't choke).

## Key Behaviors

- **Generated clocks** fold back into their master via `ClockSpec.resolve` unless `set_clock_groups -asynchronous` explicitly overrides. The master comes from `-master_clock` only; `-source` is recorded (`Clock.source`) but never infers one
- **Several clocks on one port** (rtl-buddy-cdc#330) — without `-add`, a later `create_clock` (or port-targeted `create_generated_clock`) on a port **replaces** the earlier clock: the port is stripped from it, the clock is removed once it has no port left, and a warning names both. With `-add` the port carries every clock, recorded in `ClockSpec.multi_clock_ports` (port → clocks, declaration order). The first-declared clock is the port's *primary*: `clock_for_port` returns it, so it names every flop behind the port, and it **stands for all the port's clocks** — `are_async` / `is_unreachable_crossing` expand it through `colocated_clocks`, so a crossing is synchronous only if it is synchronous (or exclusive) to every clock on the port. `async_witness` names the first asynchronous, reachable pair (deterministic, same on both backends); `filter_async` stamps it on `Crossing.async_via` and the finding gains a `[multi-clock port] asynchronous as X → Y` suffix. A non-primary clock named elsewhere (`set_input_delay -clock clk_a2`) means only itself. A multi-clock `-source` without `-master_clock`, or with one naming none of its clocks, is a G-11 warning. Internal-pin targets still hold one clock per pin (`-add` there is not modelled; warning). See the architecture article §4.5 "Multi-clock ports"
- **Internal-pin generated clocks** — when a `create_generated_clock` target is `[get_pins <hier_pin>]` rather than a top-level port, the pin path is stored in `ClockSpec.pin_clocks` and consumed by `trace_clock_root` via the `_build_bit_to_clock` helper to give each block in an internally-wired clock-forwarding chain a distinct clock identity. Pin paths use SDC convention (`u_a/clk_out`); the consumer normalises to Yosys' flattened netname (`u_a.clk_out`)
- **`-source` parsing** is shlex-tolerant: the bracketed expression after `-source` is consumed forward to the next `-` flag, so `-source [get_ports ck_a]` (split by shlex into two tokens) doesn't leak `ck_a]` into the trailing target list
- **Interface-member ports** are addressed by their **dotted name** — `[get_ports i_axi.clk]`, *not* the Yosys escaped-id form `[get_ports {\i_axi.clk }]`. The escaped-id spelling parses but no longer matches the flattened port, so the SDC silently fails to type it (the usual symptom is an `<unconstrained>` input driving a `CDC-021`/`CDC-011` cascade). Use the canonical dotted name. (See rtl-buddy-cdc#245.)
- **`set_false_path`** between clocks is treated as a pairwise async hint
- **Object-scoped / `-through` `set_false_path`** (rtl-buddy-cdc#328) — any `get_cells` / `get_pins` / `get_ports` / `get_nets` endpoint or a `-through` sends the statement to `ClockSpec.false_path_exceptions` as a `FalsePathException` (`SdcObject(kind, name, hierarchical)` patterns per side, one tuple per `-through` group, edge / check `qualifiers`, `line`, and a backend-independent canonical `statement`). Clock endpoints given alongside stay as clocks. It never adds a clock pair or changes `are_async`. A word with no object collection in it is read exactly as before (all names are clocks), so clock-pair false paths are byte-for-byte unchanged. A `-filter` in an object false path drops the statement (warning) rather than silently widening its side. The netlist side — name matching, coverage, which findings get acknowledged — is `sdc_objects.py`; see the architecture article §6.4
- **`set_max_delay -datapath_only`** between clocks (rtl-buddy-cdc#325) is an async hint too — a datapath-only bound exists only where the two clocks are not timed against each other. It is stronger than a false path: it also wins over a generated→master collapse, a shared `-group`, and an exclusive group, because the more-reporting reading keeps the crossing checked. Records land in `ClockSpec.datapath_only_delays`, clock pairs in `datapath_only_pairs`; object endpoints (`get_cells` / `get_pins` / `get_ports` / `get_nets`) are recorded as `(kind, name)` and resolved by `sdc_objects` (#328) to annotate the crossings they constrain — no suppression, no async pair; `-through` is not interpreted (warning). Plain `set_max_delay` is gated off (`_COMMAND_GATES`) and dropped exactly as before
- **`set_case_analysis`** on top-level ports (rtl-buddy-cdc#325) lands in `ClockSpec.case_analysis` (port → `"0"`/`"1"`/`"rising"`/`"falling"`, last write wins) and is consumed by the clock walker: a `$mux` / `$pmux` / `$_MUX_` whose select is a `0`/`1` case constant — directly or through buffers / inverters — follows only the selected leg ([[clock-domain-tracing]]). Bus bits must be braced (`{sel[3]}`): unbraced, the Tcl backend substitutes `[3]` as a command and the reader warns about the numeric command name. Pin / cell targets warn and are ignored; port targets that do not exist warn at analysis time (`domain.case_analysis_warnings`)
- **Exclusive groups** (`-logically_exclusive`, `-physically_exclusive`) drop crossings as unreachable in `_filter_async` before any rule sees them

## Deliberately Ignored

- **STA-only commands** (plain `set_max_delay`, `set_min_delay`, `set_load`, `set_drive`, `set_disable_timing`, …) — silently dropped at `logging.DEBUG` level. Users can point the tool at their existing constraint file without curating a CDC-only subset
- **`-filter` clauses** — on both backends; property-specific, not a name the netlist index can match
- **`set` variables / `expr` / command substitution** — evaluated by the `tcl` backend, *not* by the `tokenizer` fallback. On the fallback the first drop per file raises a `sdc.tokenizer_skipped` warning, and a worker that reports `kind: "unavailable"` raises one `sdc.tcl_unavailable` warning per run naming the fix (uv-managed Python, `python3-tkinter`, or Homebrew `python-tk@X.Y`)

## Diagnostics Policy

`ClockSpec.partial_warnings` accumulates one-line descriptions when the parser sees a CDC-relevant command it can't fully understand (e.g. `[get_clocks -filter …]`). Object patterns that match nothing in the netlist (rtl-buddy-cdc#328) are added to the same list by the CLI after `sdc_objects.resolve`. The CLI surfaces these once at the end of parsing to stderr. Truly unrecognised commands emit only `logging.DEBUG`.

## Related Pages

- [[cdc-data-model]] — `ClockSpec` and `Clock` dataclasses
- [[cdc-analysis-pipeline]] — where SDC parsing fits (stage 5)
- [[clock-domain-tracing]] — clock topology is the input to domain tracing
