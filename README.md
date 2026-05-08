# rtl-buddy-cdc

Python-based open-source CDC (Clock Domain Crossing) linting tool for RTL designs, built on top of [Yosys](https://yosyshq.net/yosys/) and designed to integrate with [rtl-buddy](https://github.com/rtl-buddy/rtl_buddy).

## Status

Early scaffolding. Not yet usable.

## Why

CDC bugs are notoriously hard to catch in simulation and devastating in silicon. Commercial CDC tools (Spyglass, Questa CDC, VC SpyGlass) are excellent but expensive and closed. The open-source EDA stack has strong synthesis (Yosys) and STA (OpenSTA) but lacks a dedicated CDC linter. `rtl-buddy-cdc` aims to fill that gap with a pragmatic ruleset, fast iteration, and a Python codebase that's easy to extend.

## Architecture

`rtl-buddy-cdc` is a **pure analyzer**. It does not invoke Yosys itself. Elaboration and netlist generation are owned by the caller (typically rtl-buddy as a preprocessing step), so the tool's input is an already-flattened netlist plus an SDC file.

```
  ┌────────────┐    yosys hierarchy;proc;flatten;write_json    ┌────────────┐
  │ rtl-buddy  │ ──────────────────────────────────────────▶  │ netlist.json│
  └────────────┘                                                └─────┬──────┘
                                                                      │
                              design.sdc ────────────────────┐        │
                                                             ▼        ▼
                                                       ┌──────────────────┐
                                                       │  rtl-buddy-cdc   │
                                                       │  (pure Python)   │
                                                       └────────┬─────────┘
                                                                ▼
                                                          violations.{json,sarif,txt}
```

Why this split:

- **Single responsibility** — the tool is a graph analyzer, not a build orchestrator.
- **No duplication** — rtl-buddy already invokes Yosys for synthesis; the same plumbing (paths, caching, error handling) is reused.
- **Frontend-swappable** — any tool that emits a compatible netlist JSON is a valid input source.
- **Faster iteration** — rule changes don't re-elaborate the design.

A thin convenience wrapper (`rtl-buddy-cdc lint --sources ... --top ...`) is provided for standalone use; it shells out to `yosys` to produce the netlist JSON and then calls the analyzer. The primary supported entry point, however, is `rtl-buddy-cdc analyze --netlist out.json --sdc design.sdc`.

## Inputs

Primary mode (`analyze`):

| Input | Required | Purpose |
|---|---|---|
| Yosys netlist JSON | yes | Flattened design (output of `write_json`) |
| **SDC** (`.sdc`) | yes (recommended) | Clock declarations + async/exclusive groups |
| Clock-spec YAML (alternative) | optional | Lightweight alternative for users without SDC |

Convenience wrapper (`lint`, standalone use only):

| Input | Required | Purpose |
|---|---|---|
| Verilog / SystemVerilog sources | yes | Design under analysis |
| Top module name | yes | Elaboration root |
| Filelist (`.f`) | optional | For large designs |

### SDC support

SDC is the primary mechanism for telling the tool which clocks exist and which pairs are asynchronous. The tool parses a focused subset of SDC sufficient for CDC analysis — STA-only commands are accepted and silently ignored.

Supported (initial target):

- `create_clock -name <name> -period <p> [get_ports <port>]`
- `create_generated_clock -name <name> -source <src> [-divide_by N | -multiply_by N | -edges {…}] <pin>`
- `set_clock_groups -asynchronous -group {…} -group {…}` — **central to CDC**: domains in different async groups are treated as crossing
- `set_clock_groups -logically_exclusive -group {…} -group {…}` — treated as not-crossing for data, optional warn
- `set_false_path -from [get_clocks A] -to [get_clocks B]` — used as crossing hints when no `set_clock_groups` is present
- `set_input_delay` / `set_output_delay` — used only to identify primary clocks on top-level ports

Ignored (parsed, not used):

- `set_max_delay`, `set_min_delay`, `set_load`, `set_drive`, `set_disable_timing`, `set_case_analysis`, etc. (timing-only)

If no SDC is supplied, the tool falls back to:

1. Heuristic detection of clock signals (signals driving the clock pin of registers post-elaboration).
2. A user-provided YAML clock-spec.
3. Single-domain assumption (all flops on one clock) — emits a warning that no CDC analysis is meaningful.

## What it checks

Initial ruleset (will expand):

- **CDC-001** — Unsynchronized data crossing (single-flop on destination clock).
- **CDC-002** — Insufficient synchronizer depth (`<2FF` for default; configurable per-port).
- **CDC-003** — Combinational logic between source flop and synchronizer first stage.
- **CDC-004** — Multi-bit data crossing without gray-coding or handshake (bus crossing).
- **CDC-005** — Reconvergent synchronizers (multiple synchronizers fed by signals from the same source domain that re-converge on the destination side).
- **CDC-006** — Glitch on a control crossing (combinational source).
- **CDC-007** — Reset crossing without async-assert / sync-deassert pattern.
- **CDC-008** — Clock used as data (clock signal in a non-clock pin).

Each violation reports: rule ID, source/destination clocks, source flop, destination flop, hierarchical path, suggested fix.

## How

The analyzer pipeline (no Yosys interaction inside the tool):

1. **Ingest netlist** — load Yosys `write_json` output; build an in-memory cell/net graph.
2. **Parse SDC** — extract clocks, generated clocks, and async/exclusive groups. Build the *clock domain graph*.
3. **Annotate domains** — propagate clock annotations from primary clocks through generated-clock pins to every register.
4. **Find crossings** — walk register-to-register fanout cones; flag any path where source and destination domains are in different async groups (or have no group relation).
5. **Apply rules** — pattern-match on each crossing's logic shape (single FF? 2FF? combinational on the way? bus width? handshake?).
6. **Report** — text/JSON/SARIF output suitable for CI gating and for `rtl-buddy` integration.

Yosys runs upstream (in rtl-buddy or in the standalone `lint` wrapper) to produce the flattened netlist JSON; the analyzer is otherwise Yosys-agnostic.

## Integration with rtl-buddy

rtl-buddy owns Yosys invocation and feeds the resulting netlist JSON to the analyzer. Sketch of the config entry (sibling of the existing synth tool entry in `root_config.yaml`):

```yaml
cfg-cdc-tools:
  - name: "rtl-buddy-cdc"
    tool: "rtl-buddy-cdc"
    opts:
      sdc: "constraints.sdc"
      sync-depth: 2
```

The corresponding `rb cdc` subcommand will:

1. Run Yosys (`hierarchy -top <top>; proc; flatten; write_json <out>`) using the same Yosys plumbing as `rb synth`.
2. Invoke `rtl-buddy-cdc analyze --netlist <out> --sdc <sdc>` and surface the report.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for environment management
- A Yosys-produced netlist JSON (`write_json`); Yosys itself is **not** a runtime dependency of the analyzer.
- A working `yosys` binary on `PATH` is only needed if you use the standalone `lint` wrapper. This workspace ships one at `../yosys/yosys`.

## Quick start

```bash
uv sync
uv run rtl-buddy-cdc --help

# Primary entry: analyze a pre-elaborated netlist
uv run rtl-buddy-cdc analyze --netlist build/design.json --sdc design.sdc

# Convenience wrapper: shells out to yosys for elaboration
uv run rtl-buddy-cdc lint --top top design/*.v --sdc design.sdc
```

## Layout

```
src/rtl_buddy_cdc/
  cli.py              # Typer entry point
  __init__.py
pyproject.toml        # project + uv config
```

## Roadmap

- [x] Scaffold project (uv, Typer)
- [ ] Yosys JSON netlist ingestion (analyzer core, primary entry)
- [ ] SDC parser (subset)
- [ ] Domain propagation
- [ ] `lint` standalone wrapper (shells out to yosys)
- [ ] CDC-001..003 (single-bit crossings)
- [ ] CDC-004 (bus crossings)
- [ ] CDC-005..008
- [ ] SARIF / JSON / text reporters
- [ ] `rtl-buddy` integration

## License

BSD 3-Clause. See [`LICENSE`](LICENSE).
