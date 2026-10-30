---
title: Crossing Detection
created: 2026-05-11
updated: 2026-10-30
type: concept
tags: [algorithm, crossing, cdc, clock-domain, netlist, bfs]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# Crossing Detection

`domain.find_crossings` is the BFS that turns "every flop is in a clock domain" into a list of `Crossing` records.

## The Walk

For each source flop with a known clock:

1. Start the BFS frontier at every bit on its `Q` pins, hop count 0
2. For each `(bit, hops)` on the frontier:
   - If `bit` connects to any flop's `D` pin in a **different** clock domain → record the crossing (or update existing record for that pair)
   - Else, push `bit` through every consumer cell that isn't a flop, emitting that cell's outputs at `hops + 1`. Flops are **skipped as transit nodes** — landing on a `D` pin is the only productive termination
3. Hop budget: **`max_hops = 4`** default. Beyond that, signals are treated as no longer "directly connecting" the two flops

## Grouping

Crossings are grouped by `(src_flop_name, dst_flop_name)` during the BFS. The first hit on a destination D-pin creates a new record; subsequent hits on the same pair **widen `width`** and **lower `min_hops`**. This is what makes multi-bit buses produce one `Crossing` with `width = N` instead of N records.

## Intentionally Excluded

- **Flop→port crossings** — when a flop drives a top-level output port directly. Responsibility shifts to the port consumer
- **Untyped-port crossings** — ports without `set_input_delay -clock` are not promoted to port-sourced `Crossing` records. CDC-006 still covers them via `_backward_fanin`
- **CLK pins as transit bits** — `_build_bit_consumers` filters out clock connections so the data BFS doesn't follow them
- **Same-domain crossings** — filtered at record-creation time when `dst_clock == src_clock`

## Pruning

Before the walk, `_make_fanout_filter` runs one bounded backward walk per clock domain from that domain's flop `D` pins, over the exact reverse of the forward edges. It records, per bit, the fewest cells to a `D` pin in each domain. The forward walk then skips expanding any bit that cannot reach a flop in a *different* domain within its remaining hop budget. Such a bit can never yield a crossing, so results are unchanged; `tests/test_find_crossings_prune.py` checks every fixture report with the prune on and off.

## Complexity

The backward pass is `O(D · E)` for D clock domains and E cell-level edges. The forward walk is `O(F · max_hops · avg_fanout)` in the worst case, where F is flop count, but only over logic within `max_hops` of a foreign-domain flop. Same-domain pipelines, usually most of a design, are skipped after their first bit.

## Related Pages

- [[cdc-data-model]] — the `Crossing` dataclass produced
- [[clock-domain-tracing]] — domain assignment consumed by the BFS
- [[cdc-analysis-pipeline]] — where crossing detection fits in the pipeline
