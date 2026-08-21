# rtl-buddy-cdc

Python-based open-source CDC (Clock Domain Crossing) linting tool for RTL designs, with a pluggable elaboration frontend ([Yosys](https://yosyshq.net/yosys/) or [slang](https://github.com/MikePopoloski/slang) via [pyslang](https://pypi.org/project/pyslang/)). Designed to integrate with [rtl-buddy](https://github.com/rtl-buddy/rtl_buddy).

## Status

Usable on IP-block-sized designs. Thirty rules implemented (CDC-001 through CDC-006, CDC-008 through CDC-023 (excl. CDC-007), plus the RDC family RDC-001 through RDC-008 — RDC-001 is the reset-crossing rule formerly known as CDC-007), recognition of the Xilinx **XPM CDC** macro family (`xpm_cdc_*`) as synchronisers with `--sync-primitive` for site-local macros, three output formats (text / JSON / SARIF), waiver-file suppression, `(* cdc_sync *)` / `(* cdc_gray *)` / `(* cdc_static *)` / `(* cdc_handshake *)` / `(* reset_sync *)` / `(* reset_polarity *)` / `(* glitchless_clock_mux *)` SV attributes for user-vetted synchronizers, gray-coded buses, runtime-constant source flops, four-phase req/ack handshake primitives, reset-synchroniser stages, reset-port polarity declarations, and glitchless clock-mux selects, structural gray-code recognition for CDC-004, and `rb cdc` / `rb cdc-regression` integration in rtl-buddy. Two elaboration frontends at parity on the regression fixture suite — Yosys (default) and slang (opt-in via the `[slang]` extra). Tested against paired *bad / good* RTL fixtures for each rule, plus a template-driven fuzz corpus (`tests/fuzz/`) and an opt-in behavioural simulation oracle (`tests/sim/`).

Known gaps and roadmap items are tracked at the end of this README.

## Why

CDC bugs are notoriously hard to catch in simulation and devastating in silicon. Commercial CDC tools are excellent but expensive and closed. The open-source EDA stack has strong synthesis (Yosys) and STA (OpenSTA) but lacks a dedicated CDC linter. `rtl-buddy-cdc` fills that gap with a pragmatic ruleset, fast iteration, and a Python codebase that's easy to extend.

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
| **SDC** (`.sdc`) | recommended | Clock declarations + async groups; without it, rule checks are skipped and the run is just a structural summary. Cross-statement clock-graph diagnostics — same-port-in-multiple-clocks, unresolved master, master cycles, duplicate clock name — are surfaced as warnings (G-11 / rtl-buddy-cdc#218). |
| Waiver file | optional | Per-violation suppression with reason (see [Waivers](#waivers)) |
| `--baseline FILE.json` | optional | Filter out findings already present in a prior JSON report; the carried-over set is surfaced as a separate tally and never drives the exit code. Useful for "fail PR only on new findings". Matches on `(rule_id, cell_name, message)`. |
| `--strict` | optional | Promote every `warning`-severity violation to `error` before reporting (see [Rules](#rules)). Exit code is unchanged — the flag is reframing, not gating. |
| `--emit-domain-map FILE.json` | optional | Write the structured clock-domain map (schema v1.0) to a sidecar file: clocks, async groups, false paths, per-flop domain assignments, typed port→clock map, and structural crossings tagged with `async_per_sdc`. Designed for downstream consumers like [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view); the normal report still runs unless paired with `--no-findings`. |
| `--emit-reset-domain-map FILE.json` | optional | Write the structured reset-domain map (schema v1.0) to a sidecar file: distinct upstream reset sources, recognised reset-synchroniser stages, per-flop reset assignments, and structural reset crossings (`async-deassert` / `polarity-mismatch` / `sync-crossing` / `comb-driven`). Parallel to `--emit-domain-map`; both can be passed in one run. See [wiki/raw/articles/rtl-buddy-cdc-reset-domain-map-schema.md](wiki/raw/articles/rtl-buddy-cdc-reset-domain-map-schema.md). |
| `--reset-hints FILE.yaml` | optional | External YAML file declaring reset-port polarity / synchroniser annotations, parallel to the in-RTL `(* reset_polarity *)` / `(* reset_sync *)` SV attributes. Hints win on disagreement with the attribute path. Requires the `[hints]` optional install extra (`pip install 'rtl-buddy-cdc[hints]'`). See [wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md](wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md). |
| `--no-findings` | optional | Skip rule evaluation entirely. Only meaningful with `--emit-domain-map` / `--emit-reset-domain-map`: the run exits 0 on successful elaboration + map emission, 2 on elaboration failure, and the normal report is suppressed. |
| `--sync-primitive MODULE` | optional | Register `MODULE` as a sanctioned CDC synchroniser primitive. A crossing landing in an instance of it is safe by construction, the instance is summarised at its destination clock instead of being declined as a multi-clock blackbox (no `CDC-BBX`), and its `DEST_SYNC_FF` parameter is checked by CDC-022. Repeatable. The Xilinx XPM CDC family is recognised built-in — use this only for an in-house or other-vendor macro. Registered names get no XPM port-naming promise, so all of their outputs are attributed to the destination clock. See [XPM CDC macro recognition](#xpm-cdc-macro-recognition). |
| `--cdc-018-depth-threshold N` | optional | Minimum sync-chain depth at which CDC-018 (cascaded synchroniser) fires. Defaults to **4** — chains of depth 2 or 3 stay silent (the textbook 2FF sync, plus a 3-stage chain common in high-MTBF designs). Raise to 5 if 4-stage chains are intentional in your design. Must be ≥ 2. |
| `--cdc-010-no-heuristic` | optional | Disable CDC-010's pin-name heuristic fallback for tech-mapped cells. By default an input pin named `E` / `EN` / `CE` / `GATE` / `SE` (case-insensitive) on a cell type outside the explicit map is treated as a control pin. Pass this flag when a library's pin naming conflicts (e.g. a vendor that uses `EN` for something other than enable) and you'd rather take the false negative than a false positive. The explicit map covering Yosys primitives and `simplemap` / `abc` gate-level cells is unaffected. |
| `--clock-trace-depth N` | optional | Maximum hop budget when tracing a flop's `CLK` net back to its top-level clock port — buffers, clock gates, muxes and divider flops each cost a hop. Defaults to **16**. A deep clock tree (a long divider / buffer / ICG chain) can exceed it and leave its downstream flops domain-unknown (visible as `summary.domain_unknown`); raise it (e.g. 40) to resolve them without a code change. Monotone: a larger budget only ever resolves **more** flops, never fewer, so the default leaves results identical. Must be ≥ 1. (rtl-buddy-cdc#263) |
| `--project-root DIR` | optional | Base directory for resolving **relative** path-bearing args (`--emit-domain-map`, `--emit-reset-domain-map`, and `--yosys-plugin` in `lint`). Precedence: this flag, else the directory of `--sdc`, else the current working directory. Set it to a stable root so those paths stay correct regardless of where the tool is launched — a driver running the tool from a nested artefact dir no longer has to hand-rebase every relative path. Absolute path args are unaffected. (rtl-buddy-cdc#245) |

Standalone wrapper (`lint`):

`lint` accepts every `analyze` reporting/analysis flag above
(`--sdc`, `--waivers`, `--strict`, `--cdc-018-depth-threshold`,
`--clock-trace-depth`, …) in addition to the elaboration inputs below.

| Input | Required | Purpose |
|---|---|---|
| Verilog / SystemVerilog sources | yes | Design under analysis |
| Top module name (`--top`) | yes | Elaboration root |
| `--frontend {yosys,slang,auto}` | optional | Elaboration frontend. `yosys` (default) shells out to `yosys` and runs `hierarchy; proc; flatten; opt_clean`. `slang` elaborates via the [pyslang](https://pypi.org/project/pyslang/) binding directly — no synth step, no Yosys runtime dependency. `auto` picks `slang` when pyslang is importable and falls back to `yosys` otherwise; useful in CI matrices where some jobs install the `[slang]` extra and others don't. Reaches parity with the Yosys frontend on every paired *bad/good* fixture in the regression suite. |
| `--yosys PATH` | optional | Yosys frontend only: override the default yosys binary lookup |
| `--yosys-plugin PATH` | optional | Yosys frontend only: load a Yosys plugin (e.g. yosys-slang's `slang.so`) and elaborate via `read_slang` for full SystemVerilog-2017 support. Falls back to the `RTL_BUDDY_SLANG_PLUGIN` environment variable when the flag is omitted; the explicit flag wins. This is the machine-local `.env` flow `rtl_buddy` populates from `.rtl-buddy/.env`. |
| `--single-unit` | optional | Yosys Slang frontend only: pass `--single-unit` to `read_slang`, compiling all sources together so preprocessor macros intentionally shared across a filelist remain visible. Requires `--yosys-plugin` (or `RTL_BUDDY_SLANG_PLUGIN`). |
| `--keep-json PATH` | optional | Yosys frontend only: save the intermediate netlist for debugging or re-runs |
| `--blackbox MODULE` | optional | Treat `MODULE` as a CDC boundary cell: keep it un-flattened (via `read_slang --blackboxed-module`) so a large subtree is analysed at its **port boundary** instead of being elaborated into the design — the #253 scaling path. **Repeatable.** Requires the yosys-slang plugin (`--yosys-plugin` / `RTL_BUDDY_SLANG_PLUGIN`). The pre-elaborated `analyze` path needs no flag — a netlist that already contains blackbox boundary modules loads transparently. See [Blackboxing for scale](#blackboxing-for-scale). |

The slang frontend is an opt-in extra. Install it alongside the package with `pip install 'rtl-buddy-cdc[slang]'` (or `uv add 'rtl-buddy-cdc[slang]'`); the default install stays Yosys-only.

## Blackboxing for scale

Fully flattening a large multi-clock block doesn't scale — the analysis walks every flop and its fan-in cone, even though the single-clock majority of the design carries no crossing (#253). `lint --blackbox MODULE` (repeatable) keeps the named module **un-flattened**, so it arrives as a boundary cell analysed at its **port boundary** rather than flop-by-flop:

- **Single-clock** blackboxed subtrees are **auto-abstracted** — summarised to their ports (each output becomes a virtual source in the subtree's clock domain, each foreign-domain input a virtual sink) and skipped. The crossings *at* the boundary are still reported; the internals are not walked. The tool decides which blackboxed subtrees are single-clock from the SDC, so you don't have to.
- A blackboxed subtree the analyzer **cannot soundly abstract** — not provably single-clock (multi-clock / unresolved), reconvergence-unsafe (≥2 crossings entering distinct input ports), or **driving a clock out of one of its output ports** — is left fully opaque and reported as a **`CDC-BBX` error** (the boundary's internal CDC is unanalysed). The run fails by default; for a subtree whose internals you intentionally don't check here (e.g. a separately signed-off IP) **waive it**: `waive CDC-BBX <instance-regex>`. This makes the coverage gap explicit rather than silent.
- A module that **generates or forwards a clock** (an output port that reaches a flop `CLK` pin, an SDC-declared clock, or another boundary's clock pin) cannot be blackboxed without the clock generation/forwarding network vanishing with it, so it is declined too — even when it is perfectly single-clock inside. The message names the offending port:
  ```text
  blackbox `u0` (`clkfwd_tile`) drives a clock output `clk_out` — clock generation/forwarding
  would be elided; flatten it or analyse standalone (waive CDC-BBX if intentionally out of
  scope here).
  ```
  This makes blackbox granularity self-guiding: on a hierarchical clock-forwarding mesh, blackboxing the *tile* is declined and you are pointed one level deeper, at the clock-output-free datapath core.
- A blackboxed instance of a **recognised CDC macro** (`xpm_cdc_*`, or a `--sync-primitive` registration) is exempt from the *not-provably-single-clock* decline: it is dual-clock *by design*, and the macro is the synchroniser. (The clock-output decline still applies — a macro that forwards a clock elides the clock network like any other module.) It is summarised at its destination clock instead — see [XPM CDC macro recognition](#xpm-cdc-macro-recognition).
- `analyze` consumes a pre-elaborated netlist and needs no flag — blackbox boundary modules already present in the JSON load transparently.

**It is opt-in — without `--blackbox`, behavior is unchanged.** On a normally-flattened netlist with no blackbox modules there is nothing to abstract, so the analyzer walks the full flat design exactly as in prior releases (the pre-existing fixture suite is unchanged proof). The abstraction is conservative: exact for registered single-clock logic, and it over-approximates but **never under-reports** on combinational paths through a boundary — see [`wiki/raw/articles/rtl-buddy-cdc-architecture.md`](wiki/raw/articles/rtl-buddy-cdc-architecture.md) §4.8–§4.9 for the precision model.

## XPM CDC macro recognition

Real FPGA designs rarely hand-roll a 2FF chain — they instantiate a vendor CDC macro. The Xilinx **XPM CDC** library (UG974) is the dominant case, and its seven members are recognised **built-in**:

`xpm_cdc_single` · `xpm_cdc_array_single` · `xpm_cdc_gray` · `xpm_cdc_handshake` · `xpm_cdc_pulse` · `xpm_cdc_sync_rst` · `xpm_cdc_async_rst`

A crossing whose destination lands in one of these is **safe by construction** — the macro *is* the synchroniser — so it is accepted rather than flagged, and the instance is summarised at its resolved `dest_clk` domain instead of being declined as a multi-clock blackbox.

**Why by module name rather than by elaborating the macro's internals.** XPM sources ship inside the vendor install tree (`$XILINX_VIVADO/data/ip/xpm`) and Vivado injects them at synthesis; a filelist assembled from project RTL does **not** contain them, so the analyzer sees a bodyless, dual-clock blackbox. Requiring elaboration would mean the feature only worked for users who went hunting for vendor sources. The name is the contract instead: `xpm_cdc_*` is a fixed, documented, versioned library whose port naming is rigid — every data/control port is `src_*` or `dest_*`, every clock pin is `src_clk` / `dest_clk` — and that regularity is enough to summarise the macro correctly without seeing one line of its body.

**Recognition is not a blind spot.** Each output port is stamped with the domain that *really* drives it (`dest_*` outputs at `dest_clk`, `xpm_cdc_handshake.src_rcv` at `src_clk`), so a `dest_out` consumed by a flop in some **third** domain is still reported by the ordinary `dst_clock != src_clock` test. The crossing the macro handles is accepted; the one it doesn't is kept. An instance whose destination clock cannot be identified (an undeclared or unresolved `dest_clk`) is **not** vouched for — it falls through to the generic path and is declined as before.

**Depth stays checkable.** The macro's stage count is the `DEST_SYNC_FF` parameter (plus `SRC_SYNC_FF` on `xpm_cdc_handshake`), not a chain CDC-002 can walk. [`CDC-022`](#rules) reads the parameter and fires when it is below `--sync-depth`.

**Site-local macros** are registered with the repeatable `--sync-primitive MODULE`:

```bash
rtl-buddy-cdc analyze -n design.json -s design.sdc \
    --sync-primitive acme_cdc_sync --sync-depth 3
```

Registered names get the same treatment minus the XPM port-naming promise, so all of their outputs are attributed to the destination clock (the conservative reading).

**If you *do* have the XPM sources in your filelist**, no flag is needed: XPM tags its internal stages `(* ASYNC_REG = "TRUE" *)`, and the `async_reg` entry in `USER_SYNC_ATTRS` matches attribute names case-insensitively, so the flattened internals are recognised through the ordinary [`(* cdc_sync *)`](#sv-attributes) machinery.

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
| **CDC-001** | error | Unsynchronized control crossing — destination flop has no second-stage synchronizer (chain depth = 1). Fires on flop→flop and (when `set_input_delay -clock <c>` types the source) port→flop crossings. Suppressed on `(* cdc_static *)` sources and `(* cdc_handshake *)` capture registers (a single dst register is the intended capture in a req/ack handshake). The depth walk recognises a synchroniser coded as a single **packed shift register** (`reg [N-1:0] s; s <= {s[N-2:0], d}`, tapped from `s[N-1]`) — it lowers to one multi-bit `$dff` whose stages shift intra-cell — and counts its effective depth, so the packed form is accepted identically to the separate-flop form (issue #264). |
| **CDC-002** | warning (`--strict` → error) | Insufficient synchronizer depth — chain present but shorter than the project's `--sync-depth` (default 2 = silent; raise to 3+ for high-speed / low-MTBF designs). Fires on flop→flop and typed port→flop crossings. The packed shift-register synchroniser shape (see CDC-001) is depth-counted here too. |
| **CDC-003** | error | Combinational logic between source flop and synchronizer first stage — gate output can glitch and be sampled |
| **CDC-004** | error | Multi-bit bus crossing without recognized gating or gray-coding. Three gating shapes are accepted: a `$mux` driving the destination flop's `D` with a dst-domain select (handshake load shape), the same mux behind up to two transparent fanout buffers (`$buf` / `$_BUF_` / `$_NOT_` / `$not` / `$pos`), or a `$dffe`-style flop-with-enable whose `EN` fanin is all dst-domain. Gray-coded crossings are accepted structurally (canonical `g = b ^ (b >> 1)` pattern) and via the explicit `(* cdc_gray *)` escape hatch |
| **CDC-005** | warning (`--strict` → error) | Reconvergent synchronizers — one source flop fans out to multiple sync chains *and* the synchronized outputs reconverge downstream. The phase-2 forward-cone filter (issue #33) rules out structurally-redundant-but-harmless fanout: two sync chains feeding disjoint downstream registers no longer fire. |
| **CDC-006** | error | Glitchy combinational source — synchronizer is fed by combinational logic with no registering flop, reaching unregistered top-level ports. Suppressed when `set_input_delay -clock <my_clk>` types the port into the destination flop's own clock domain (port is asserted same-domain). |
| **RDC-001** | error | Async reset crossing — flop's `ARST` is driven by a flop in a different async clock domain, no reset synchronizer. Violations are grouped by the shared async source: one report per source listing every destination flop it feeds (the typical reset-distribution-tree shape). *Renamed from `CDC-007` (issue #107); existing waivers written against `CDC-007` continue to suppress via the legacy-id alias in `rtl_buddy_cdc.waivers`.* |
| **RDC-002** | error | Reset polarity mismatch on a direct flop→flop **async** reset — consumer's `ARST_POLARITY` doesn't match the producer's `ARST_VALUE`, so the consumer never enters reset when the producer does. Fires on direct (no inverter between) flop→flop reset paths only, and only on async-reset (`$adff*`) consumers — sync-reset (`$sdff*`) signals are intentional gating, not part of the async distribution tree (that concern lives with **RDC-003**). Suppressed when the consumer is recognised as a reset-synchroniser chain member (the user may have built an intentional polarity-inverting sync). Findings are grouped by `(producer, polarities)` so a single upstream wiring bug feeding N consumers becomes one report listing every affected destination. |
| **RDC-003** | error | Sync reset crossing without a reset synchroniser — flop's `SRST` is driven (directly or through comb logic) by a flop in a different async clock domain. Sync resets are sampled on the destination clock's rising edge; cross-domain sources can be metastable on the sample cycle. Findings are grouped by `(src_flop, src_clk, dst_clk)` so a single foreign-domain source feeding many sync-reset consumers becomes one report (parallel to RDC-001's reset-tree grouping). Classic fix: 2FF reset synchroniser in the destination clock domain between the foreign source and the consumer. |
| **RDC-004** | error | Reset pin driven by combinational logic with no synchroniser in the path — flop's async reset is the output of a comb gate (`$and`/`$or`/`$mux`/etc.) whose backward fanin reaches one or more flops. Comb outputs can glitch when inputs transition asynchronously, producing spurious reset assertions on the consumer. Fires on `$adff*` consumers only (sync resets filter glitches at the clock edge); pure comb-of-ports (e.g. `rst_a_n & test_mode_n`) is RDC-005's domain. Classic fix: register the comb output on the consumer's clock before using as a reset. |
| **RDC-005** | warning | Multiple reset sources converging on a flop without explicit muxing — flop's async reset is a comb-AND/OR of 2+ distinct top-level reset ports, with no `$mux`/`$pmux` selecting which source is active. Both resets are simultaneously active and the user has no control over which dominates. Complementary to RDC-004: fires precisely on the comb-of-ports case RDC-004 deliberately skips. Severity `warning` — the AND-of-resets pattern is common in SoC designs; the rule invites review rather than declaring an unambiguous bug. Suppressed when the immediate driver cell is `$mux`/`$pmux` (explicit-muxing exemption). |
| **RDC-008** | error | Unsynced primary-reset-port deassertion — flop's async reset is driven *directly* by a top-level input port (`ResetDomain.reset.source == "port"`) and the flop is not part of a recognised reset-synchroniser chain in its own clock domain. RDC-001 is the symmetric rule for foreign-domain flop-sourced resets; RDC-008 fills the port-source gap. Reset assertion is fine (combinational), but deassertion is unsynchronised to the consumer clock — recovery/removal timing violations can leave subsets of flops in different reset states. **Asymmetric-intent detection**: only fires when the user has built a sync chain for the port in *some* clock domain but missed it in another (and the missing-chain domain has ≥2 unsynced consumers). Stays silent on designs that use the raw port directly everywhere (a common simplification in small RTL). Suppressed on recognised reset-sync chain members, `(* reset_sync *)`-marked flops, sync-reset (`SRST`) consumers, and non-port reset sources. |
| **RDC-006** | warning | Muxed async reset without a local synchroniser — flop's async reset is driven directly by a `$mux`/`$pmux` and the flop is not part of a recognised reset-synchroniser chain. RDC-005's mux exemption assumes the user picked a source intentionally, but the selected reset's deassertion edge is still asynchronous to the consumer clock. Fills the RDC-005 gap by requiring a 2FF reset synchroniser in the consumer clock domain between the mux and downstream consumers. Suppressed on recognised reset-sync chain members and on flops marked `(* reset_sync *)`. |
| **RDC-007** | error | Reset-synchroniser chain accepted with deassertion-polarity wired backwards — a structurally-valid reset-sync chain (constant-fed head, same-domain Q→D, shared async reset, ≥2 flops) whose head's `D` constant matches the *asserted* polarity instead of the deasserted one. Active-low chains must load `1'b1` on the deassertion edge (active-high: `1'b0`); a chain that loads the asserted value instead is a one-shot that never releases, and every downstream consumer using the chain's output as their `ARST` stays held in reset forever. Severity `error` — the failure is functional, not stylistic, and the structural recogniser would otherwise silently exempt all downstream consumers from RDC-001..-006. Limitation: `(* reset_sync *)`-marked chains whose head isn't constant-fed are not checkable here. |
| **CDC-008** | error | Clock signal used as data — clock-network bit reaches a non-CLK input (flop `D`/`ARST`, comb input, etc.); cells that themselves drive a flop CLK are exempted (legitimate ICG / clock muxes / dividers) |
| **CDC-009** | warning | Pulse-width / fast-to-slow data loss — single-bit src-domain pulse may land entirely between two slower dst-clock rising edges and never be sampled. Fires when the SDC declares both clock periods, `src_period × 1.5 < dst_period`, and the src flop's `D` pin matches the textbook edge-detector pattern `A & ~A_d` (with `A_d` the 1-cycle delay of `A`). False-negative-biased: handshake / pulse-stretcher / toggle-sync idioms naturally fall outside the pattern and stay silent. |
| **CDC-010** | error | Glitch on the clock network from a wrong-domain control signal — dual of CDC-008. Fires when a clock-network cell's *control* pin (clock-mux select `$mux.S`, ICG enable `$dffe.EN` / `$dlatch.EN`) is driven by a flop whose clock domain is asynchronous to every one of the cell's own clock-input domains. The async control transition can chop the output clock into runt pulses on every downstream flop and is not recoverable by a synchronizer at the sink. Suppression composes naturally: a control routed through a `(* cdc_sync *)` first stage into one of the gated-clock domains lands in that domain and stays silent; `set_clock_groups -asynchronous` puts the domains in different async groups, while leaving the control's domain in a *same* group as one of the gated clocks asserts synchronous and suppresses via `ClockSpec.are_async`. Coverage spans (1) Yosys higher-level cells (`$mux` / `$dffe` / `$dlatch`), (2) Yosys gate-level cells emitted by `simplemap` / `abc` (`$_MUX_` / `$_MUX{4,8,16}_` / `$_DLATCH_*` / `$_DFFE_*` / `$_SDFFE_*` — all variant explosion absorbed by prefix paths so no per-polarity enumeration), and (3) tech-mapped library cells via a conservative pin-name heuristic: an input pin named `E` / `EN` / `CE` / `GATE` / `SE` (case-insensitive) on an otherwise-unknown cell type is treated as a control pin. Pass `--cdc-010-no-heuristic` to disable the heuristic on libraries whose naming conflicts. |
| **CDC-011** | warning / error | Unconstrained primary input captured by clocked logic — top-level input port has no `set_input_delay -clock <name>` typing yet physically reaches a flop's `D` pin or its synchronous-reset (`SRST`) pin. The `SRST` destinations are walked directly rather than read off the crossing list, because crossings only ever sink on `D` pins — without that walk, whether an untyped sync reset got reported depended on whether the synthesis pass folded it into a `$dff` reset mux or into a dedicated `$sdff` `SRST` pin (rtl-buddy-cdc#272). Async reset pins (`ARST` / `CLR` / `ALOAD`) are out of scope — they are legitimately untimed, and the RDC family owns their failure modes. Fires as `warning` when the port lands in a single destination clock domain (the fix is usually adding SDC typing); escalates to `error` when the same port lands in **two or more** distinct domains (a single port cannot be synchronous to multiple clocks — intrinsically wrong regardless of SDC opinion). One violation per source port, listing every destination clock. |
| **CDC-012** | warning | Functional data-hold on a gated multi-bit crossing — bus crossing passes CDC-004's gated-bus exemption (mux-on-D with sync'd select or `$dffe` with sync'd `EN`), but nothing keeps the source payload stable while the enable's sync chain is in flight. A payload change between request and capture silently corrupts the latched value. Detection: a multi-bit gated crossing whose source flop's register-neighbourhood has no path back to a `dst_clock` flop (no synced-back handshake reachable from this crossing's source). The feedback check is per-crossing, so an unrelated handshake between the same clocks no longer silences a broken crossing. Fix is a req/ack handshake that holds the payload until ack returns. Gray-coded sources (structural `g = b ^ (b >> 1)` shape or `(* cdc_gray *)` annotation) are exempt — at most one bit changes per src cycle, so any dst sample is coherent. |
| **CDC-013** | warning | Fast-to-slow control-event loss on a toggle synchroniser — src-domain flop's `D` pin matches the toggle-with-enable pattern `D = en ? ~Q : Q` and `src_period × 1.5 < dst_period`. Two events between destination samples cancel to zero edges, silently losing both. Pairs with CDC-009 (raw-pulse case); the two rules partition the fast-to-slow data-loss class by `D`-pin shape. Severity `warning` — designs that rate-limit events at the application level use this pattern correctly; the rule invites review. Handshake / counter-with-backpressure shapes whose `D` isn't the `Q`/`~Q` mux fall outside the classifier and stay silent; a toggle that *does* match (e.g. the `ip_cdc_handshake` req toggle) is suppressed by `(* cdc_handshake *)` on the toggle reg. |
| **CDC-014** | error | Combinational logic between synchroniser stages — distinct from CDC-003 (comb feeding stage 1): here a comb cell sits between `stage_1.Q` and `stage_2.D` on the same clock. The gate's propagation delay can sample a metastable head output, destroying the full-cycle resolution time the chain was meant to provide. The chain walker terminates at depth = 1 (the gate breaks the flop→flop continuation), so CDC-001 would mis-report "no second-stage synchronizer" — CDC-001 defers via `_chain_has_inter_stage_comb` when a follow-on flop is present behind a gate, and CDC-014 fires with the correct framing. Suppressed on `(* cdc_sync *)` chain heads and `(* cdc_handshake *)` capture registers (post-capture decode comb is ordinary datapath). |
| **CDC-015** | error | Synchroniser chain asynchronously reset from a foreign clock domain — a 2FF (or deeper) sync chain in `dst_clk` whose flops carry an `ARST` driven by a flop in a clock domain async to `dst_clk`. The chain's resolving flops are released asynchronously to their own clock; the synchroniser cannot reach steady state. CDC-001 / CDC-002 see a structurally valid chain and stay silent — the failure is in the chain's reset path. CDC-shaped framing complements RDC-001's reset-tree framing on the same physical structure: the two findings are intentionally independent so users can act on either. Suppressed on `(* cdc_sync *)` chain heads. |
| **CDC-018** | warning | Cascaded synchroniser smell — a CDC crossing's destination-domain sync chain depth reaches `--cdc-018-depth-threshold` (default 4). The chain still works (extra latency, slightly worse MTBF tail), but the depth is a code-review smell — classically caused by two engineers each adding their own 2FF sync on the same wire, or a refactor leaving the original chain in place when a new wrapper was added. Severity `warning` — surfaces the smell without forcing a fix. Detection walks each crossing's dst flop through `_sync_chain_depth` (which only follows pure flop→flop same-domain single-reader hops); groups by `(src_flop, dst_clock)` so a sliced bus doesn't multi-fire. Suppressed when the chain head is marked `(* cdc_sync *)`; the threshold can be raised via `--cdc-018-depth-threshold` for high-MTBF designs that intentionally run deeper chains. |
| **CDC-019** | warning | Independently-synced one-hot decode across CDC — N≥2 single-bit source-domain flops sharing a common combinational driver (a one-hot decoder, priority arbiter, case-statement output, etc.) each have an async crossing to flops in the same destination clock domain. CDC-004 misses this because each source flop is structurally 1-bit; the lanes are *related* in the comb logic upstream, but the multi-bit-bus detector only sees N independent 1-bit crossings. The destination resolves each lane on its own schedule, so transitions where ≥2 source bits change simultaneously can be sampled as intermediate combinations the encoder never emits (e.g. ``4'b0010`` mid-transition between ``0001`` and ``0100``). Severity `warning` — sometimes intentional when the destination only reads one bit or a handshake gates the sample. Detection groups by ``(driver_comb_cell, dst_clock)``; suppressed when any source flop is marked `(* cdc_gray *)` / `(* cdc_static *)` / `(* cdc_sync *)` or when the immediate driver is itself a flop. |
| **CDC-020** | warning | Sliced-bus reconvergence across CDC — a genuinely-multi-bit source flop (WIDTH≥2) has its bits sliced into N≥2 width=1 crossings that each independently cross to flops in the same destination clock domain. CDC-004 misses this shape because each per-lane crossing's width is 1 — the multi-bit-bus detector's `width <= 1` skip drops every lane even though the source bus is genuinely multi-bit. Sibling of CDC-019: same per-lane-independent-sync hazard, but the source is a true multi-bit register rather than a shared comb decoder. Severity `warning` — sometimes intentional. Detection groups by `(src_flop, dst_clock)`; suppressed via `(* cdc_gray *)` / `(* cdc_static *)` / `(* cdc_handshake *)` on the source (the handshake payload is held stable across the req→ack window, so no lane is sampled mid-flight), or via the same structural gray-encode-into-multi-bit-sync exemption CDC-004 uses. |
| **CDC-BBX** | error | Blackbox boundary not analysed — emitted by the analyzer's blackbox handling (not the rule pack) when a blackboxed subtree **cannot be soundly abstracted** and is therefore left opaque: it is not provably single-clock (≥2 distinct clock roots, or an unresolved clock), or one of its **output ports drives a clock** (clock generation / forwarding that abstraction would elide), or it is reconvergence-unsafe (≥2 crossings entering distinct input ports, whose internal reconvergence the boundary star-collapse would hide — see [Blackboxing for scale](#blackboxing-for-scale)). The boundary's internal CDC is unanalysed, so this is a **coverage gap**, surfaced per instance and at `error` so it fails by default. Intentional opacity (a separately signed-off IP) is acknowledged by waiving it: `waive CDC-BBX <instance-regex>`. Not produced when a subtree *is* abstracted (single-clock, ≤1 incoming crossing) — those boundary crossings are analysed normally. |
| **CDC-021** | error | Flop CLK driven by undeclared top-level port — a flop's `CLK` pin traces back to a top-level input port that has no `create_clock` declaration in the SDC. Pairs with CDC-011 (data-pin equivalent). The failure mode is silent: an undeclared port-as-clock doesn't appear in any `set_clock_groups -asynchronous` declaration, so `are_async` returns False against every declared clock and `_filter_async` drops every crossing involving the undeclared domain — the rest of the rule pack stays completely silent on flops in that domain. CDC-021 surfaces the methodology bug (missing `create_clock`) so the user can declare the clock and let the other rules do their job. Detection groups by `(undeclared_port, list_of_consumer_flops)`. Skipped when no SDC was supplied. |
| **CDC-022** | warning (`--strict` → error) | Recognised CDC primitive with insufficient synchroniser depth. A sanctioned CDC macro (the `xpm_cdc_*` family, or a `--sync-primitive` registration) carries its stage count as a **parameter** — `DEST_SYNC_FF`, plus `SRC_SYNC_FF` on `xpm_cdc_handshake` — not as a flop chain the analyzer can walk. Once the macro is recognised as a synchroniser its crossing stops being reported, so without this rule a project requiring `--sync-depth 3` would silently accept every `DEST_SYNC_FF=2` instance. CDC-022 restores the check at the only place the depth is visible: the instance parameter. Reads only `Cell.type` / `Cell.parameters`, so it fires whether or not a blackbox sibling module was loaded for the macro. An XPM instantiation that leaves the parameter at its default is checked against UG974's documented default of 4 (Yosys records only *overridden* parameters on a cell); a site-registered primitive with no override is skipped — there is no known default to assume. Severity mirrors CDC-002: a 2-stage synchroniser is correct engineering at most clock rates, so the finding is "shallower than *this project* asked for", not "broken". |
| **CDC-023** | warning (`--strict` → error) | Clock net driven by a **combine** of two declared clocks. A clock gate (`$and` / `$or` / `$xor` …) or a clock-path transparent latch (`$dlatch` / `$_DLATCH_*`) whose legs carry two or more *distinct declared clocks* mixes clock domains combinationally: the resulting net glitches, runs at no declared frequency, and gives every flop behind it an ambiguous domain. The clock-root tracer has declined on this shape since rtl-buddy-cdc#263, which stopped the silent mislabel but left the flops in the generic `domain_unknown` tally with no cause attached; CDC-023 names the cause — the combining **cell**, the combined **net**, and the two **clocks** — so it can be fixed or waived instead of hunted for. Findings are produced from the tracer's own decline events (`domain.find_clock_combines`), so the rule fires exactly when the tracer declines. A clock **mux** never fires: a mux *selects* one clock rather than combining them. Neither does a normal ICG — a plain enable port is not a declared clock, and nor is the `<unconstrained>` sentinel on an untyped input port. Severity `warning`: combining two clocks is nearly always a bug, but the shape also covers deliberate, characterised test/debug clock chopping, and a new rule id shouldn't turn a previously-clean run into a hard error on landing. |
| **CDC-016** | error | Opposite-edge synchroniser halves MTBF — adjacent stages of a sync chain sample on different edges of the same clock (e.g. stage 1 `posedge dst_clk`, stage 2 `negedge dst_clk`). Each metastable value has only half a clock period to resolve, roughly halving the mean-time-between-failure. RTL looks syntactically symmetric, so CDC-001 / CDC-002 see a valid chain and stay silent. Detection walks `_sync_chain_flops` from each crossing's destination flop and compares Yosys `CLK_POLARITY` parameters (and gate-level `$_DFF_N_` / `$_DFF_P_` cell-type variants) for adjacent pairs. Suppressed when the chain head is `(* cdc_sync *)`-marked — user vouches for the chain shape. Multi-bit crossings are deferred (the chain walker keys off a 1-bit head). |
| **CDC-017** | error | Transparent latch in CDC path — a `$dlatch` / `$_DLATCH_*` cell sitting between a source flop in clock domain A and a destination flop in clock domain B != A. During the latch's enable-active phase, a metastable source signal propagates transparently to the destination flop's D pin; the latch provides no metastability resolution time at all. Without this rule the entire CDC bug is silent — `find_crossings` keys off flop-to-flop fanin and doesn't traverse latches, so no rule fires on the shape. Detection walks every latch cell, identifies the dst flop reading its Q and the src flop driving its D (via `_backward_flop_fanin` through any intermediate comb), and fires when src_clock != dst_clock. Suppressed when the destination flop is marked `(* cdc_sync *)`. |

Each violation carries:
- rule ID and severity
- a human-readable message
- the offending crossing (when applicable)
- a source location (file + line/column) parsed from the cell's `attributes["src"]`

## Unsupported patterns

rtl-buddy-cdc is a flop-based analyzer — the BFS walker traces nets between `$dff*` / `$adff*` / `$sdff*` / `$dffsr*` cells. Crossings that flow through structures the walker does not model are silently invisible to the rule pack:

- **Dual-port memory crossings** (write port on clock A, read port on clock B). Yosys keeps inferred memories as `$memwr_v2` / `$memrd` cells; the analyzer does not walk across that boundary. Use the vendor memory-compiler CDC report for this shape, or black-box the macro in a CDC-only build with a behavioural model that uses register storage. Pinned by `tests/fixtures/unsupported_dualport_ram_crossing/` as a regression sentinel (issue #176).

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
- **JSON** — structured, includes summary counts, full crossing/violation lists, and source locations. Stable schema for downstream consumers (rtl-buddy itself, custom dashboards). Every violation also carries an `instance_path: list[str]`; a top-level `by_instance` summary aggregates kept violations by path. `summary.domain_unknown` (with a bounded `domain_unknown_flops` sample) reports flops whose clock root could not be traced, and an additive `inferred_clock_candidates` list flags undeclared internal nets that drive ≥4 flop `CLK` pins (a likely-forgotten `create_generated_clock`). Both are report-only diagnostics — they never change a domain, crossing, or violation. (rtl-buddy-cdc#263)
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

## In-RTL pragmas

A suppression can also be written next to the RTL it applies to, as a magic comment in the `rbcdc:` namespace (each rtl-buddy tool owns one — `rbsch:`, `rbxeno:`, …; there is no SV-attribute form and no alternative spelling):

```systemverilog
// rbcdc: disable-rule CDC-001
// rbcdc: disable-rule CDC-001,CDC-002  hand-reviewed handshake
/* rbcdc: disable-rule CDC-005 library cell */
```

Grammar: `// rbcdc: disable-rule <RULE-ID>[,<RULE-ID>…] [reason …]`, one pragma per line, in a `//` or `/* … */` comment. Each rule id in the list becomes its own waiver, scoped to the file the pragma is written in, with the free text after the rule list as the reason.

Sources are scanned as **text** — never through Yosys or slang — so a pragma costs nothing and needs no frontend.

> **This release ships the scanner only** (`rtl_buddy_cdc.pragma.scan`): pragmas are parsed into waiver records but not yet applied to a run. Wiring them into `lint` lands with the follow-on (issue #42), block-scoped `enable-rule` with issue #43.

## SV attributes

Mark a flop as a user-vetted synchronizer first stage by attaching an attribute to the wire/reg it drives:

```sv
(* cdc_sync *) logic dst_q;             // canonical synchronizer first stage
(* synchronizer *) logic dst_q;         // alias
(* async_reg = "TRUE" *) logic dst_q;   // common synthesis-attribute alias
(* cdc_gray *) logic [N-1:0] src_bus;   // source bus is gray-coded
(* gray_code *) logic [N-1:0] src_bus;  // alias
(* cdc_static *) logic [N-1:0] cfg_q;   // quasi-static source (config bit / mode reg)
(* quasi_static *) logic [N-1:0] cfg_q; // alias
(* cdc_handshake *) logic [N-1:0] payload; // reg in a four-phase req/ack handshake
(* req_ack_handshake *) logic req;      // alias
(* reset_sync *) logic rst_sync;        // vetted reset-synchroniser stage
(* reset_synchronizer *) logic rst_sync;// alias
```

Reset-port polarity declaration (on an input port):

```sv
(* reset_polarity = "low"  *) input logic rst_n;   // active-low reset port
(* reset_polarity = "high" *) input logic rst;     // active-high reset port
```

`cdc_sync` / aliases mark a flop as a vetted synchronizer first stage — skipped by CDC-001, -002, -003, and -006. CDC-004 (bus crossings) and CDC-005 (reconvergence) still fire — those failure modes don't depend on individual sync-shape correctness.

`cdc_gray` / `gray_code` mark a source bus as gray-coded so CDC-004 accepts it as a safe multi-bit crossing without needing the structural detector to find the canonical XOR-shift shape.

`cdc_static` / `quasi_static` mark a source flop as runtime-constant — programmed once at boot and held during the operating window, e.g. configuration registers, mode bits, calibration values. Suppresses CDC-001, -002, -003, and -004 on crossings whose source is the tagged flop (no metastability can occur on a non-transitioning value). CDC-005 (reconvergence) stays live — reconvergent fanout of a static signal merged with a non-static signal is still a coherent hazard.

`cdc_handshake` / `req_ack_handshake` mark a register as a participant in a sanctioned four-phase req/ack vector-CDC handshake (the `ip_cdc_handshake` primitive shape). The analyzer checks CDC *structure*, so a correct handshake otherwise trips four rules on its protected paths — all false positives by protocol. Tag the source toggle, the held payload, and the destination capture register, and the rule keyed at each is suppressed: CDC-013 on the toggle (the source is backpressured until ack — no event can be lost), CDC-020 on the payload (held stable across the req→ack→done window — no sliced lane can be sampled mid-flight), CDC-001 on the capture (a single dst register is the intended capture under `dst_valid`, not a missing second stage), and CDC-014 on post-capture decode comb (ordinary datapath, not a gate wedged between sync stages). Mark the blessed primitive once and every instance is recognised, retiring the per-instance waivers (issue #247).

`reset_sync` / `reset_synchronizer` mark a flop as a vetted reset-synchroniser stage — the structural recogniser in `rtl_buddy_cdc.reset_domain.find_reset_synchronizers` deliberately requires a constant-fed chain head, so chains whose head's D is fed by an upstream signal (rather than a literal constant) would normally be missed. RDC-002 / RDC-004 / RDC-005 skip flops marked with this attribute and skip consumers whose ARST is driven by a marked flop's Q.

`reset_polarity` declares a top-level reset port's active polarity. RDC-002 reads this declaration as authoritative and fires when a flop's inferred reset-pin polarity (Yosys derives it from `posedge` / `negedge` on the port) disagrees with the declaration — the classic "designer added a `posedge` flop on a port the rest of the design treats as active-low" wiring bug.

`glitchless_clock_mux` / `glitchless_mux` / `glitchfree_clock_mux` mark a clock-mux select wire as user-vouched glitchless. CDC-010 normally proposes "synchronise the select onto one of the gated clocks" as the fix — but that would actually break a correctly-built glitchless mux (cross-coupled-latch envelope or a foundry library cell) by introducing a single-clock dependency that defeats the other-clock-aware gating. The attribute is the user's explicit "trust me, the surrounding mux topology handles the safe handoff" promise; CDC-010 stays silent on the marked select.

Yosys preserves SV attributes on the netname rather than the cell, so the analyzer maps tagged bits back to the originating flop's `Q` pin (or, for `reset_polarity`, to the input port itself).

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
  primitives.py # Sanctioned CDC macro registry (xpm_cdc_*) + depth params
  waivers.py    # Waiver file parser + apply()
  pragma.py     # In-RTL `// rbcdc:` pragma scanner
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
    xpm_cdc_*/           # XPM CDC macro recognition (clean case + third-domain guard)
  test_*.py
```

## Roadmap

Implemented:

- [x] Scaffold project (uv, Typer)
- [x] Yosys JSON netlist ingestion
- [x] SDC parser (`create_clock`, `set_clock_groups -asynchronous`)
- [x] Clock-domain tracing through buffers / ICGs / clock muxes / dividers
- [x] Flop→flop crossing detection (single-bit + bus, deduped per pair)
- [x] CDC-001 through CDC-006, CDC-008, CDC-009, CDC-010 (full coverage — Yosys primitives, gate-level `simplemap` / `abc` output, and tech-mapped library cells via a pin-name heuristic with `--cdc-010-no-heuristic` opt-out — gate-level `$_DFF*` flop visibility landed in rtl-buddy-cdc#194), CDC-011 (unconstrained primary inputs), CDC-012 through CDC-017 (toggle / latch / opposite-edge / inter-stage-comb / chain-reset hazards), CDC-018 (cascaded synchroniser smell — closes G-2 from rtl-buddy-cdc#188), CDC-019 (independently-synced one-hot decode — closes G-4 from rtl-buddy-cdc#188), CDC-020 (sliced-bus reconvergence — closes G-6 from rtl-buddy-cdc#188), CDC-021 (undeclared port driving a flop CLK — closes G-10 from rtl-buddy-cdc#188), CDC-023 (clock net driven by a combine of two declared clocks — rtl-buddy-cdc#269), RDC-001 through RDC-008 (the full reset-domain rule family — #107 / #114; RDC-001 is the reset-crossing rule formerly known as CDC-007; RDC-007 deassertion-polarity check landed in rtl-buddy-cdc#202; RDC-008 unsynced primary-reset-port closes G-8 from rtl-buddy-cdc#188)
- [x] `lint` standalone wrapper (yosys → analyzer)
- [x] Text / JSON / SARIF reporters with source locations
- [x] Waiver file suppression
- [x] `(* cdc_sync *)`, `(* cdc_gray *)`, and `(* cdc_static *)` SV-attribute support
- [x] XPM CDC macro recognition (`xpm_cdc_*` family recognised by module name as synchronisers; `--sync-primitive` for site-local macros; CDC-022 checks the `DEST_SYNC_FF` depth parameter; uppercase `(* ASYNC_REG *)` case-fold — rtl-buddy-cdc#275)
- [x] Gray-coded bus recognition for CDC-004 (canonical `g = b ^ (b >> 1)` structural detection + multi-bit 2FF chain at the destination)
- [x] Paired positive (`good_*`) fixtures for every implemented rule
- [x] rtl-buddy `rb cdc` / `rb cdc-regression` integration (lives in the rtl_buddy repo)
- [x] Configurable CDC-002 sync depth via `--sync-depth N` (also accepted as `sync-depth:` under `cfg-cdc-tools` opts)
- [x] SDC: `create_generated_clock` (with transitive master resolution), logically/physically-exclusive groups, `set_false_path -from/-to` as async hints, `set_input_delay` / `set_output_delay` for port-side domain inference. End-of-parse warnings for partially-understood CDC-relevant commands.
- [x] CDC-006 port-side: when `set_input_delay -clock <c>` types a port, CDC-006 suppresses the port if it resolves to the destination flop's own clock and reports the source clock by name when it differs.
- [x] First-class port→flop crossings: `find_crossings(module, port_clock=...)` emits port-sourced `Crossing` records for ports the SDC has typed via `set_input_delay`. CDC-001 and CDC-002 now fire on those (CDC-003 defers to CDC-006's existing port-comb walk; CDC-004 / CDC-005 skip them as flop-source-specific concepts).
- [x] RDC-001 (was CDC-007) reset-tree grouping: violations are merged by `(src_flop, src_clk, dst_clk)` — a single async-reset source feeding many destinations produces one violation listing every destination, instead of N near-duplicates.
- [x] **slang frontend** — elaborate SystemVerilog via [pyslang](https://pypi.org/project/pyslang/) as a peer to the Yosys frontend, swappable via `lint --frontend slang`. Covers flop inference (async-reset shape), combinational primitive lowering (binary / unary / conditional / element-select / range-select / concatenation / replication), `always_comb`, hierarchical instance flattening with port aliasing, SV attribute propagation, and Yosys-style `src` source-location attributes on every emitted cell (`file:line.col-line.col`, surfaced by the JSON / SARIF reporters). Reaches parity with the Yosys frontend on every SDC-equipped fixture in the regression suite. Opt-in via the `[slang]` install extra (`pip install 'rtl-buddy-cdc[slang]'`); the default install stays `typer`-only.
- [x] **Hierarchical reporting** (#46) — every violation carries an `instance_path: tuple[str, ...]` derived from its Yosys-flatten or slang cell name, populated at the CLI boundary so the rule pack stays frontend-agnostic. Text reporter buckets findings under per-instance headers inside each rule group (`[top]` / `u_block_a / u_sync`), collapsing to the flat layout when every finding in the rule group lives at top. JSON output gains `instance_path: list[str]` on every violation / suppressed / baseline_carryover entry plus a top-level `by_instance` aggregation of kept violations. SARIF gains `logicalLocations` with `fullyQualifiedName` on each result whose instance path is non-empty, alongside the existing `physicalLocation`. All additive — `JSON_CONTRACT` keys and `--baseline` match key are untouched. See [`wiki/raw/articles/hierarchical-reporting.md`](wiki/raw/articles/hierarchical-reporting.md) for the design.
- [x] **Domain-map emission** (#106) — `--emit-domain-map FILE.json` writes a stable v1.0 JSON artifact (`schema_version: "1.0"`) capturing the analyzer's clock-domain view: clocks + generated clocks + clock groups + false-path pairs, per-flop domain assignments with source locations, the typed port→clock map, and structural crossings tagged with `async_per_sdc`. Pair with `--no-findings` to skip rule evaluation and produce just the map. Consumed by [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view) for the clock-domain hierarchy overlay.
- [x] **Reset-domain-map emission** (#108) — `--emit-reset-domain-map FILE.json` writes a parallel v1.0 artifact capturing the reset-tree view: distinct upstream reset sources (port / inferred / constant) with `(* reset_polarity *)` declarations, recognised reset-synchroniser stages, per-flop reset assignments, and structural reset crossings (`async-deassert` / `polarity-mismatch` / `sync-crossing` / `comb-driven`). Composable with `--emit-domain-map` in a single run; the two artefacts share the `design.top` envelope so consumers can join them safely. See [`wiki/raw/articles/rtl-buddy-cdc-reset-domain-map-schema.md`](wiki/raw/articles/rtl-buddy-cdc-reset-domain-map-schema.md).
- [x] **External reset hints (`--reset-hints`)** (#129) — opt-in YAML file declaring reset-port polarity and synchroniser annotations, parallel to the in-RTL `(* reset_polarity *)` / `(* reset_sync *)` SV attributes. Same vocabulary, external file when the user can't touch RTL (vendor IP, generated wrappers, multi-block boards). Hints win on disagreement with the attribute path. Gated on the `[hints]` install extra (`pip install 'rtl-buddy-cdc[hints]'`) — default installs stay `typer`-only; PyYAML pulls in only when this extra is requested. Mirrors the slang frontend's `[slang]`-extra pattern. See [`wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md`](wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md).

Not yet:

- [ ] CDC-006 refinements — comb-source severity tuning (downgrade for paths that hit a registered output before leaving the module)
- [ ] CDC-007 refinements — recognise multi-source reset synchronizer trees and shared reset distribution networks
- [ ] DFT / scan-mode awareness — exempt scan_en, scan_in, test-mode controls from CDC checks under a configurable scan-mode pragma
- [ ] In-RTL pragma comments (`// rbcdc: disable-rule …`, in-file block suppression) for inline waiving without an external file — scanner landed (see [In-RTL pragmas](#in-rtl-pragmas)); application and block scoping pending
- [ ] Instance-scoped waivers (`waive CDC-001 inst:u_block_a/.*`) — natural follow-on to hierarchical reporting now that `instance_path` is on every violation
- [ ] Glitch detection on data path through async muxes / clock-gate enables

## License

BSD 3-Clause. See [`LICENSE`](LICENSE).
