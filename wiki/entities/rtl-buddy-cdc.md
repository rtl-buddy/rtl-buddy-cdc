---
title: rtl-buddy-cdc
created: 2026-05-11
updated: 2026-05-14
type: entity
tags: [cdc, clock-domain, synchronization, pipeline, cli, integration, yosys, slang, frontend]
sources: [raw/articles/rtl-buddy-cdc-architecture.md]
confidence: high
---

# rtl-buddy-cdc

A Python-based CDC (clock-domain-crossing) linter that consumes a flattened `Module` — produced by either the Yosys or the slang [[elaboration-frontends|frontend]] — plus an SDC file, and emits text / JSON / SARIF reports. It catches eight classic CDC bug shapes (CDC-001 through CDC-008) in flattened RTL.

## Goals

- Catch CDC-001 through CDC-008 with reasonable false-positive and false-negative rates
- Surface findings in formats that drop into existing review and CI workflows (text / JSON / SARIF)
- Keep the elaboration frontend swappable so the rule pack doesn't depend on a specific toolchain
- Stay small enough to fork and extend in a sitting

## Explicit Non-Goals

- **Not a synthesis tool.** The rule pack never invokes a synthesizer on its primary path — it consumes an in-memory `Module` produced by a [[elaboration-frontends|frontend]]. The `analyze` command takes a pre-elaborated Yosys JSON; the `lint` wrapper drives a frontend (Yosys or slang) for source-to-analysis convenience.
- **Not a timing tool.** SDC is parsed for clock topology and async partitioning only; numeric delays and slack are ignored.
- **Not a Tcl interpreter.** The SDC parser is a deliberate `shlex`-based pattern matcher, not a real Tcl host.
- **Not a hierarchical analyzer.** Today the netlist must be fully flattened (which both frontends produce). Hierarchical reporting is on the roadmap but not architecturally present.

## Module Map

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

`__init__.py` exposes a thin `main()` shim that delegates to `cli.app` (used by the console-script entry point). The other public API is `frontend.Frontend` + `frontend.elaborate(sources, top, frontend=…)`, which `cli.lint` consumes when the user supplies SV sources.

## Integration with rtl-buddy

`rtl_buddy` (the broader CLI) owns Yosys invocation, model resolution, and result aggregation. `rtl-buddy-cdc` is invoked as a **subprocess** through `tools/cdc_rtl_buddy.py`, which:

1. Resolves the model's filelist via the same `VlogFilelist` plumbing used by `rb synth`
2. Calls `rtl-buddy-cdc lint` once with `--format text` and once with `--format json`
3. Forwards `--sync-depth` and `--waivers` when configured

The subprocess boundary is intentional: it lets `rtl_buddy` pick up new analyzer releases via `uv sync` without code changes, and lets the analyzer evolve its Python API without breaking the integration. The trade-off is that startup cost is paid twice per analysis (text + JSON).

### CLI contract (breaking changes ripple downstream)

- **Flags consumed today:** `--netlist`, `--sdc`, `--top`, `--waivers`, `--sync-depth`, `--format`, `--output`
- **JSON schema consumed today:** `summary.violations` (int), `summary.suppressed` (int), `summary.crossings` (int)
- **Exit codes:** 0 = clean/fully waived, 1 = unsuppressed violations, 2 = lint-only frontend-elaboration failure (Yosys binary missing, slang pyslang missing, or pyslang diagnostics fatal)

## Performance

Designed for **interactive run times on individual IP blocks**, not full-SoC scale. On the alu_accel design (~90 flops, ~545 cells) a full lint run is well under one second.

Key complexity: `find_crossings` is `O(F · max_hops · avg_fanout)`, the rule pack is `O(C)` for most rules except CDC-005 (`O(C²)`) and CDC-008 (walks cell graph from every flop CLK).

## Related Pages

- [[elaboration-frontends]] — Yosys + slang frontends behind `--frontend`; the contract every frontend produces
- [[cdc-analysis-pipeline]] — the full pipeline from elaborated `Module` to report
- [[cdc-data-model]] — all dataclasses and schemas
- [[waivers-and-reporting]] — output formats and waiver system
- [[cdc-testing-strategy]] — fixtures, tests, extension points
