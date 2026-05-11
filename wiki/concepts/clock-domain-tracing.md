---
title: Clock Domain Tracing
created: 2026-05-11
updated: 2026-05-11
type: concept
tags: [algorithm, clock-domain, cdc, netlist, flops]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# Clock Domain Tracing

`domain.trace_clock_root` is the heart of CDC-008 and the foundation for all domain assignment. Every flop's domain identity comes from chasing its CLK net upstream until a top-level input port is hit.

## Cell-Type Categories

The walker recognizes four categories of clock-network cells:

| Category | Yosys cell types | Behavior |
|---|---|---|
| **Buffer / inverter** | `$not`, `$logic_not`, `$buf`, `$pos`, `$reduce_bool`, `$_BUF_`, `$_NOT_` | Transparent: trace the single `A` input |
| **Two-input gate (ICG)** | `$and`, `$or`, `$logic_and`, `$logic_or`, `$xor`, `$xnor` | Trace both inputs; the side resolving to a clock is the root, the other is the gate's enable |
| **Mux** | `$mux`, `$pmux` | Trace each candidate input; first to resolve wins (can't statically know `S`) |
| **FF (clock divider)** | any `FF_CELL_TYPES` via `Q` output | Recurse on the source flop's CLK pin; divided clock inherits domain from upstream root |

## Walk Mechanics

- **Per-call `seen` set** keyed on bit ID prevents infinite loops
- **`max_depth` = 16** (default) — intentionally low; clock networks rarely exceed a handful of hops, and deep walks are more likely following data than clock
- **`_bit_drivers`** is precomputed once and shared across all `trace_clock_root` calls

## Internal-Pin Generated Clocks

The walker also accepts an optional `bit_to_clock: dict[Bit, str]` short-circuit map, supplied by `assign_domains(module, pin_clocks=...)` and built from `ClockSpec.pin_clocks` via `_build_bit_to_clock`. When the walk lands on a bit that's in the map, it returns that clock name immediately instead of continuing back to a top-level port.

This is what models SoC clock-forwarding chains where each block declares its forwarded clock with `create_generated_clock` at an internal pin (e.g. `[get_pins u_a/clk_out]`). Without this hop, every flop downstream of `u_a`'s clock buffer would collapse to whichever top input port feeds `u_a` — and the SDC's per-block clock declarations would be inert at integration scope.

`_build_bit_to_clock` normalises the SDC pin convention (`u_a/clk_out`, `/`-separated) to Yosys' flattened netname convention (`u_a.clk_out`, `.`-separated) before looking up the netname's bits. If two generated clocks target the same net, first-writer-wins via `setdefault` — the SDC is internally inconsistent and we don't try to repair it.

The returned clock name is a generated-clock identity (e.g. `ck_b0`), not a top-level port. Downstream consumers (`_filter_async`, `are_async`) handle this transparently because `ClockSpec.resolve()` already collapses generated clocks back to their root master.

## Role in CDC-008

CDC-008 uses the same walker to compute the set of cells that drive a flop CLK (`_clock_network_cells()`). Cells flagged as clock-network are **exempt** from CDC-008 ("clock signal used as data") because legitimate ICGs, clock muxes, and dividers all read clocks as inputs.

## Related Pages

- [[cdc-data-model]] — `FlopDomain` dataclass produced by tracing
- [[crossing-detection]] — BFS that consumes domain assignments
- [[cdc-rule-pack]] — CDC-008's use of `_clock_network_cells()`
