# rtl-buddy-cdc

Python-based open-source CDC (Clock Domain Crossing) linting tool for RTL designs, with a pluggable elaboration frontend ([Yosys](https://yosyshq.net/yosys/) or [slang](https://github.com/MikePopoloski/slang) via [pyslang](https://pypi.org/project/pyslang/)). Designed to integrate with [rtl-buddy](https://github.com/rtl-buddy/rtl_buddy).

## Status

Usable on IP-block-sized designs. Eight rules implemented (CDC-001 through CDC-008), three output formats (text / JSON / SARIF), waiver-file suppression, `(* cdc_sync *)` / `(* cdc_gray *)` SV attributes for user-vetted synchronizers and gray-coded buses, structural gray-code recognition for CDC-004, and `rb cdc` / `rb cdc-regression` integration in rtl-buddy. Two elaboration frontends at parity on the regression fixture suite — Yosys (default) and slang (opt-in via the `[slang]` extra). Tested against paired *bad / good* RTL fixtures for each rule.

Known gaps and roadmap items are tracked at the end of this README.

## Why

CDC bugs are notoriously hard to catch in simulation and devastating in silicon. Commercial CDC tools (Spyglass, Questa CDC, VC SpyGlass) are excellent but expensive and closed. The open-source EDA stack has strong synthesis (Yosys) and STA (OpenSTA) but lacks a dedicated CDC linter. `rtl-buddy-cdc` fills that gap with a pragmatic ruleset, fast iteration, and a Python codebase that's easy to extend.

## Architecture

For the full reference (data model, pipeline, rule helpers, extension points), see [`wiki/raw/articles/rtl-buddy-cdc-architecture.md`](wiki/raw/articles/rtl-buddy-cdc-architecture.md). The summary below is the elevator pitch.

`rtl-buddy-cdc` is a **pure analyzer**. The core `analyze` entry point takes a pre-elaborated netlist (an in-memory `Module`) — elaboration is not part of the analyzer's primary responsibility. The standalone `lint` wrapper drives a pluggable frontend (Yosys subprocess **or** pyslang in-process) for source-to-analysis convenience.

```
  SV sources + --top              pre-elaborated
        │                          netlist.json
        ▼                                │
  ┌─────────────────┐                    │
  │ frontend.elaborate                   │
  │   ├─ yosys (default)                 │
  │   │   hierarchy;proc;flatten;        │
  │   │   write_json                     │
  │   └─ slang (pyslang)                 │
  │       elaborate in-process           │
  └────────┬────────┘                    │
           ▼                              ▼
                      Module (in-memory)
                            │
       design.sdc ──────────┤
       cdc.waivers (opt) ───┤
                            ▼
                  ┌──────────────────┐
                  │  rtl-buddy-cdc   │
                  │  (pure Python)   │
                  └────────┬─────────┘
                           ▼
                  report.{txt,json,sarif}
```

Why this split:

- **Single responsibility** — the rule pack is a graph walker, not a build orchestrator.
- **Frontend-pluggable** — any tool that produces a Yosys-shape `Module` is a valid input source. Two frontends ship today; adding a third (e.g. Verilator-based) is a localised change. See [`wiki/concepts/elaboration-frontends.md`](wiki/concepts/elaboration-frontends.md).
- **No duplication** — when called via `rb cdc`, rtl-buddy's existing Yosys plumbing (paths, caching) is reused.
- **Faster iteration** — rule changes don't re-elaborate the design.

## Quick start

```bash
uv sync
uv run rtl-buddy-cdc --help

# Primary entry: analyze a pre-elaborated netlist
uv run rtl-buddy-cdc analyze \
    --netlist build/design.json \
    --sdc design.sdc

# Standalone wrapper: shells out to yosys for elaboration
uv run rtl-buddy-cdc lint \
    --top my_top \
    --sdc design.sdc \
    rtl/*.sv

# Machine-readable output (JSON or SARIF)
uv run rtl-buddy-cdc analyze \
    --netlist build/design.json --sdc design.sdc \
    --format sarif --output cdc.sarif

# With a waiver file for hand-reviewed exceptions
uv run rtl-buddy-cdc analyze \
    --netlist build/design.json --sdc design.sdc \
    --waivers cdc.waivers
```

Exit codes:
- `0` — clean, or every violation suppressed by a waiver
- `1` — at least one unsuppressed rule violation
- `2` — `lint` only: yosys elaboration failed

## Inputs

Primary mode (`analyze`):

| Input | Required | Purpose |
|---|---|---|
| Yosys netlist JSON | yes | Flattened design (output of `write_json`) |
| **SDC** (`.sdc`) | recommended | Clock declarations + async groups; without it, rule checks are skipped and the run is just a structural summary |
| Waiver file | optional | Per-violation suppression with reason (see [Waivers](#waivers)) |
| `--baseline FILE.json` | optional | Filter out findings already present in a prior JSON report; the carried-over set is surfaced as a separate tally and never drives the exit code. Useful for "fail PR only on new findings". Matches on `(rule_id, cell_name, message)`. |
| `--strict` | optional | Promote every `warning`-severity violation to `error` before reporting (see [Rules](#rules)). Exit code is unchanged — the flag is reframing, not gating. |

Standalone wrapper (`lint`):

| Input | Required | Purpose |
|---|---|---|
| Verilog / SystemVerilog sources | yes | Design under analysis |
| Top module name (`--top`) | yes | Elaboration root |
| `--frontend {yosys,slang,auto}` | optional | Elaboration frontend. `yosys` (default) shells out to `yosys` and runs `hierarchy; proc; flatten; opt_clean`. `slang` elaborates via the [pyslang](https://pypi.org/project/pyslang/) binding directly — no synth step, no Yosys runtime dependency. `auto` picks `slang` when pyslang is importable and falls back to `yosys` otherwise; useful in CI matrices where some jobs install the `[slang]` extra and others don't. Reaches parity with the Yosys frontend on every paired *bad/good* fixture in the regression suite. |
| `--yosys PATH` | optional | Yosys frontend only: override the default yosys binary lookup |
| `--keep-json PATH` | optional | Yosys frontend only: save the intermediate netlist for debugging or re-runs |

The slang frontend is an opt-in extra. Install it alongside the package with `pip install 'rtl-buddy-cdc[slang]'` (or `uv add 'rtl-buddy-cdc[slang]'`); the default install stays Yosys-only.

## SDC support

The parser is a focused subset — STA-only commands (`set_max_delay`, `set_min_delay`, `set_load`, `set_drive`, `set_disable_timing`, `set_case_analysis`, etc.) are silently ignored. CDC-relevant commands recognised today:

- `create_clock -name <name> -period <p> [get_ports <port>]`
- `create_generated_clock -name <n> -master_clock <m> -source <pin> -divide_by N [get_pins <pin>]` — generated clock is treated as synchronous to its master unless the SDC explicitly overrides via `set_clock_groups -asynchronous`
- `set_clock_groups -asynchronous -group {…} -group {…}` — load-bearing for the entire rule pass
- `set_clock_groups -logically_exclusive` / `-physically_exclusive` — clocks in different exclusive groups never coexist at runtime, so the analyzer drops the apparent crossing as unreachable before the rule pack sees it
- `set_false_path -from [get_clocks A] -to [get_clocks B]` — equivalent to declaring `A` and `B` async for CDC purposes (path-specific `-through` is not interpreted)
- `set_input_delay -clock <c> [get_ports <p>]` / `set_output_delay -clock <c> [get_ports <p>]` — assigns top-level data ports to a clock domain
- Comments (`#`), backslash line continuation (`\`)

When the parser sees a CDC-relevant command it can't fully understand (e.g. `set_false_path -through`, `[get_clocks -filter …]`), it accumulates a one-line warning and surfaces them all at the end of the run rather than spamming line-by-line. Truly unknown commands (`set_max_delay`, `set_load`, …) are silently dropped at DEBUG level.

If no SDC is supplied, the tool prints a structural summary and skips all rule checks.

## Rules

| ID | Severity | What it catches |
|---|---|---|
| **CDC-001** | error | Unsynchronized control crossing — destination flop has no second-stage synchronizer (chain depth = 1). Fires on flop→flop and (when `set_input_delay -clock <c>` types the source) port→flop crossings. |
| **CDC-002** | warning (`--strict` → error) | Insufficient synchronizer depth — chain present but shorter than the project's `--sync-depth` (default 2 = silent; raise to 3+ for high-speed / low-MTBF designs). Fires on flop→flop and typed port→flop crossings. |
| **CDC-003** | error | Combinational logic between source flop and synchronizer first stage — gate output can glitch and be sampled |
| **CDC-004** | error | Multi-bit bus crossing without recognized gating or gray-coding. Three gating shapes are accepted: a `$mux` driving the destination flop's `D` with a dst-domain select (handshake load shape), the same mux behind up to two transparent fanout buffers (`$buf` / `$_BUF_` / `$_NOT_` / `$not` / `$pos`), or a `$dffe`-style flop-with-enable whose `EN` fanin is all dst-domain. Gray-coded crossings are accepted structurally (canonical `g = b ^ (b >> 1)` pattern) and via the explicit `(* cdc_gray *)` escape hatch |
| **CDC-005** | warning (`--strict` → error) | Reconvergent synchronizers — one source flop fans out to multiple sync chains *and* the synchronized outputs reconverge downstream. The phase-2 forward-cone filter (issue #33) rules out structurally-redundant-but-harmless fanout: two sync chains feeding disjoint downstream registers no longer fire. |
| **CDC-006** | error | Glitchy combinational source — synchronizer is fed by combinational logic with no registering flop, reaching unregistered top-level ports. Suppressed when `set_input_delay -clock <my_clk>` types the port into the destination flop's own clock domain (port is asserted same-domain). |
| **CDC-007** | error | Async reset crossing — flop's `ARST` is driven by a flop in a different async clock domain, no reset synchronizer. Violations are grouped by the shared async source: one report per source listing every destination flop it feeds (the typical reset-distribution-tree shape). |
| **CDC-008** | error | Clock signal used as data — clock-network bit reaches a non-CLK input (flop `D`/`ARST`, comb input, etc.); cells that themselves drive a flop CLK are exempted (legitimate ICG / clock muxes / dividers) |

Each violation carries:
- rule ID and severity
- a human-readable message
- the offending crossing (when applicable)
- a source location (file + line/column) parsed from the cell's `attributes["src"]`

## Pipeline

1. **Ingest netlist** — load Yosys `write_json` output; build an in-memory cell/net graph (`netlist.py`).
2. **Identify flops** — recognise the 11 Yosys `$dff*` / `$adff*` / `$sdff*` / `$dffsr*` cell variants (`flops.py`).
3. **Trace clock domains** — for each flop's `CLK` net, walk back through buffers, inverters, integrated clock gates, clock muxes, and clock dividers to find the originating top-level port (`domain.trace_clock_root`).
4. **Find crossings** — BFS the combinational fanout from each flop's `Q` bits; record every flop→flop path that lands across domains. Group by `(src_flop, dst_flop)` so multi-bit buses and reconvergent fanout collapse to one record with `width` and `min_hops` (`domain.find_crossings`).
5. **Parse SDC** — extract clocks and async groups. Filter the structural crossings to those in async-grouped pairs.
6. **Apply rules** — each rule is a small function in `rules.py` registered in the `RULES` dict; new rules are a one-line addition.
7. **Apply waivers** — split violations into "kept" and "suppressed by waiver" (`waivers.apply`).
8. **Report** — dispatch to the chosen formatter (`reporter.render_text` / `render_json` / `render_sarif`).

## Output formats

`--format text|json|sarif` (default `text`), `--output PATH` to write to a file.

- **Text** — human-readable summary suitable for terminals and CI logs. Inside each rule group, violations are bucketed by hierarchical instance path (`[top]` / `u_block_a / u_sync`); the bucketing collapses to a flat layout when every finding in the rule group lives at the top instance.
- **JSON** — structured, includes summary counts, full crossing/violation lists, and source locations. Stable schema for downstream consumers (rtl-buddy itself, custom dashboards). Every violation also carries an `instance_path: list[str]`; a top-level `by_instance` summary aggregates kept violations by path.
- **SARIF 2.1.0** — GitHub-Code-Scanning-compatible. `tool.driver.rules` populated for every rule that fired in the run; results carry `physicalLocation.region` and, when the violation lives inside a child instance, a `logicalLocations` entry whose `fullyQualifiedName` is the dot-joined instance path. Suppressed (waived) findings are emitted with a SARIF `suppressions` field so the alert exists but doesn't fail the build.

To inspect SARIF locally without uploading, the easiest paths are the [SARIF Viewer VS Code extension](https://marketplace.visualstudio.com/items?itemName=MS-SarifVSCode.sarif-viewer) or the browser-based [Microsoft SARIF web viewer](https://microsoft.github.io/sarif-web-component/).

## Waivers

Per-violation suppression in a small text file (`cdc.waivers` by convention):

```
# Comments and blank lines are ignored.
waive CDC-001 .*procdff\$9.*       hand-reviewed by jsmith
waive CDC-005 .*known_good_sync.*  library cell, see issue #42
waive *       .*generated_codegen.* tool-emitted
```

Format: `waive <RULE-ID|*> <regex> [reason ...]`

The regex is matched against the offending cell name, the canonical `"src_flop -> dst_flop"` text (when there's a crossing), and the violation message; a hit on any one suppresses. The first matching waiver wins. Suppressed findings are still reported (with the matching reason and waiver-line number) but don't drive the exit code, so a fully-waived run returns 0.

## SV attributes

Mark a flop as a user-vetted synchronizer first stage by attaching an attribute to the wire/reg it drives:

```sv
(* cdc_sync *) logic dst_q;             // canonical synchronizer first stage
(* synchronizer *) logic dst_q;         // alias
(* async_reg = "TRUE" *) logic dst_q;   // Vivado-compatible alias
(* cdc_gray *) logic [N-1:0] src_bus;   // source bus is gray-coded
(* gray_code *) logic [N-1:0] src_bus;  // alias
```

`cdc_sync` / aliases mark a flop as a vetted synchronizer first stage — skipped by CDC-001, -002, -003, and -006. CDC-004 (bus crossings) and CDC-005 (reconvergence) still fire — those failure modes don't depend on individual sync-shape correctness.

`cdc_gray` / `gray_code` mark a source bus as gray-coded so CDC-004 accepts it as a safe multi-bit crossing without needing the structural detector to find the canonical XOR-shift shape.

Yosys preserves SV attributes on the netname rather than the cell, so the analyzer maps tagged bits back to the originating flop's `Q` pin.

## Integration with rtl-buddy

`rtl-buddy` owns Yosys invocation and feeds the resulting netlist JSON to the analyzer. Two subcommands are exposed:

- `rb cdc -c lint/cdc/cdc.yaml` — run all CDC analyses listed in the config.
- `rb cdc-regression -c lint/cdc/cdc_regression.yaml` — regression-style runner over multiple suites.

A CDC config entry looks like:

```yaml
# lint/cdc/cdc.yaml
rtl-buddy-filetype: cdc_config
analyses:
  - name: "ip_cdc_handshake_lint"
    desc: "CDC lint of the handshake IP"
    model: "ip_cdc_handshake"
    model_path: "../../design/common/models.yaml"
    tool: "rtl-buddy-cdc"
    constraints: "ip_cdc_handshake.sdc"
    waivers: "ip_cdc_handshake.waivers"   # optional
```

`rb cdc` resolves the model, runs Yosys (`hierarchy -top <top>; proc; flatten; opt_clean; write_json <out>`) using the same plumbing as `rb synth`, then invokes `rtl-buddy-cdc analyze`. Working examples live under [rtl-buddy-project-template](https://github.com/rtl-buddy/rtl-buddy-project-template/tree/main/lint/cdc).

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for environment management
- For `analyze`: a Yosys-produced netlist JSON (`write_json`); Yosys itself is **not** a runtime dependency of the analyzer.
- For `lint`: a working `yosys` binary on `PATH` (or via `--yosys`).

## Layout

```
src/rtl_buddy_cdc/
  cli.py        # Typer entry points (analyze, lint, version)
  netlist.py    # Yosys write_json loader (Module/Cell/Port/Netname)
  flops.py      # Recognise the Yosys FF cell zoo
  domain.py     # Clock-root tracing + flop→flop crossing detection
  sdc.py        # Minimal SDC parser
  rules.py      # CDC-001..-008 + RULES registry
  waivers.py    # Waiver file parser + apply()
  reporter.py   # text / JSON / SARIF formatters
tests/
  fixtures/
    bad_*/      # negative cases — each rule has at least one
    good_*/     # paired positive counterparts (textbook fixes)
    {good,bad}_source_sync_chain/     # source-sync methodology (separate clk_in per block)
    {good,bad}_source_sync_internal/  # source-sync with internal-pin create_generated_clock
    ip_cdc_handshake/    # canonical golden vendored from rtl-buddy-project-template
    clock_gating/        # ICG positive case
    marked_user_sync/    # (* cdc_sync *) attribute coverage
  test_*.py
```

## Roadmap

Implemented:

- [x] Scaffold project (uv, Typer)
- [x] Yosys JSON netlist ingestion
- [x] SDC parser (`create_clock`, `set_clock_groups -asynchronous`)
- [x] Clock-domain tracing through buffers / ICGs / clock muxes / dividers
- [x] Flop→flop crossing detection (single-bit + bus, deduped per pair)
- [x] CDC-001 through CDC-008
- [x] `lint` standalone wrapper (yosys → analyzer)
- [x] Text / JSON / SARIF reporters with source locations
- [x] Waiver file suppression
- [x] `(* cdc_sync *)` and `(* cdc_gray *)` SV-attribute support
- [x] Gray-coded bus recognition for CDC-004 (canonical `g = b ^ (b >> 1)` structural detection + multi-bit 2FF chain at the destination)
- [x] Paired positive (`good_*`) fixtures for every implemented rule
- [x] rtl-buddy `rb cdc` / `rb cdc-regression` integration (lives in the rtl_buddy repo)
- [x] Configurable CDC-002 sync depth via `--sync-depth N` (also accepted as `sync-depth:` under `cfg-cdc-tools` opts)
- [x] SDC: `create_generated_clock` (with transitive master resolution), logically/physically-exclusive groups, `set_false_path -from/-to` as async hints, `set_input_delay` / `set_output_delay` for port-side domain inference. End-of-parse warnings for partially-understood CDC-relevant commands.
- [x] CDC-006 port-side: when `set_input_delay -clock <c>` types a port, CDC-006 suppresses the port if it resolves to the destination flop's own clock and reports the source clock by name when it differs.
- [x] First-class port→flop crossings: `find_crossings(module, port_clock=...)` emits port-sourced `Crossing` records for ports the SDC has typed via `set_input_delay`. CDC-001 and CDC-002 now fire on those (CDC-003 defers to CDC-006's existing port-comb walk; CDC-004 / CDC-005 skip them as flop-source-specific concepts).
- [x] CDC-007 reset-tree grouping: violations are merged by `(src_flop, src_clk, dst_clk)` — a single async-reset source feeding many destinations produces one violation listing every destination, instead of N near-duplicates.
- [x] **slang frontend** — elaborate SystemVerilog via [pyslang](https://pypi.org/project/pyslang/) as a peer to the Yosys frontend, swappable via `lint --frontend slang`. Covers flop inference (async-reset shape), combinational primitive lowering (binary / unary / conditional / element-select / range-select / concatenation / replication), `always_comb`, hierarchical instance flattening with port aliasing, SV attribute propagation, and Yosys-style `src` source-location attributes on every emitted cell (`file:line.col-line.col`, surfaced by the JSON / SARIF reporters). Reaches parity with the Yosys frontend on every SDC-equipped fixture in the regression suite. Opt-in via the `[slang]` install extra (`pip install 'rtl-buddy-cdc[slang]'`); the default install stays `typer`-only.
- [x] **Hierarchical reporting** (#46) — every violation carries an `instance_path: tuple[str, ...]` derived from its Yosys-flatten or slang cell name, populated at the CLI boundary so the rule pack stays frontend-agnostic. Text reporter buckets findings under per-instance headers inside each rule group (`[top]` / `u_block_a / u_sync`), collapsing to the flat layout when every finding in the rule group lives at top. JSON output gains `instance_path: list[str]` on every violation / suppressed / baseline_carryover entry plus a top-level `by_instance` aggregation of kept violations. SARIF gains `logicalLocations` with `fullyQualifiedName` on each result whose instance path is non-empty, alongside the existing `physicalLocation`. All additive — `JSON_CONTRACT` keys and `--baseline` match key are untouched. See [`wiki/raw/articles/hierarchical-reporting.md`](wiki/raw/articles/hierarchical-reporting.md) for the design.

Not yet:

- [ ] CDC-006 refinements — comb-source severity tuning (downgrade for paths that hit a registered output before leaving the module)
- [ ] CDC-007 refinements — recognise multi-source reset synchronizer trees and shared reset distribution networks
- [ ] DFT / scan-mode awareness — exempt scan_en, scan_in, test-mode controls from CDC checks under a configurable scan-mode pragma
- [ ] In-RTL pragma comments (`// rtl-buddy-cdc disable-rule …`, Spyglass-style block suppression) for inline waiving without an external file
- [ ] Instance-scoped waivers (`waive CDC-001 inst:u_block_a/.*`) — natural follow-on to hierarchical reporting now that `instance_path` is on every violation
- [ ] Pulse-width / fast-to-slow data-loss checks (CDC-009-class: data on src_clk shorter than one dst_clk period)
- [ ] Glitch detection on data path through async muxes / clock-gate enables

## License

BSD 3-Clause. See [`LICENSE`](LICENSE).
