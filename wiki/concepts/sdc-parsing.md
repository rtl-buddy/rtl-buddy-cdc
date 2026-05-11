---
title: SDC Parsing
created: 2026-05-11
updated: 2026-05-11
type: concept
tags: [sdc, eda, parsing, clock-domain, design-decision]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# SDC Parsing

`sdc.py` is intentionally **not a Tcl interpreter**. SDC is Tcl syntactically, but the CDC-relevant subset is small enough that a hand-rolled `shlex` tokenizer is the right tool. Real Tcl interpreters (`tkinter.Tcl()`) execute user code, complicate deployment, and add a non-Python dependency.

## Supported Commands

```
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

Plus: `#` comments, `\` line continuation, and permissive flag-skipping for unrecognised options on otherwise-known commands (vendor-specific dialects don't choke).

## Key Behaviors

- **Generated clocks** fold back into their master via `ClockSpec.resolve` unless `set_clock_groups -asynchronous` explicitly overrides
- **Internal-pin generated clocks** — when a `create_generated_clock` target is `[get_pins <hier_pin>]` rather than a top-level port, the pin path is stored in `ClockSpec.pin_clocks` and consumed by `trace_clock_root` to give each block in an internally-wired clock-forwarding chain a distinct clock identity. Pin paths use SDC convention (`u_a/clk_out`); the consumer normalises to Yosys' flattened netname (`u_a.clk_out`)
- **`-source` parsing** is shlex-tolerant: the bracketed expression after `-source` is consumed forward to the next `-` flag, so `-source [get_ports ck_a]` (split by shlex into two tokens) doesn't leak `ck_a]` into the trailing target list
- **`set_false_path`** between clocks is treated as a pairwise async hint
- **Exclusive groups** (`-logically_exclusive`, `-physically_exclusive`) drop crossings as unreachable in `_filter_async` before any rule sees them

## Deliberately Ignored

- **STA-only commands** (`set_max_delay`, `set_min_delay`, `set_load`, `set_drive`, `set_disable_timing`, `set_case_analysis`, …) — silently dropped at `logging.DEBUG` level. Users can point the tool at their existing constraint file without curating a CDC-only subset
- **Tcl constructs** beyond `[get_clocks …]` / `[get_ports …]` / `[get_pins …]` — no `set` variables, `expr`, `-filter` clauses, or `set_false_path -through`

## Diagnostics Policy

`ClockSpec.partial_warnings` accumulates one-line descriptions when the parser sees a CDC-relevant command it can't fully understand (e.g. `set_false_path -through`, `[get_clocks -filter …]`). The CLI surfaces these once at the end of parsing to stderr. Truly unrecognised commands emit only `logging.DEBUG`.

## Related Pages

- [[cdc-data-model]] — `ClockSpec` and `Clock` dataclasses
- [[cdc-analysis-pipeline]] — where SDC parsing fits (stage 5)
- [[clock-domain-tracing]] — clock topology is the input to domain tracing
