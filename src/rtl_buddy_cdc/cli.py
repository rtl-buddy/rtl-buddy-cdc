from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import IO

import typer

from rtl_buddy_cdc import (
    netlist,
    render as render_mod,
    reporter,
    sdc as sdc_mod,
    waivers as waivers_mod,
)
from rtl_buddy_cdc.abstract import instance_clock_pins
from rtl_buddy_cdc.domain import (
    Crossing,
    assign_domains,
    find_crossings,
    find_inferred_clock_candidates,
)
from rtl_buddy_cdc.hierarchy import (
    compose_boundaries,
    reconvergence_unsafe_instances,
)
from rtl_buddy_cdc.clock_network import find_clock_network_crossings
from rtl_buddy_cdc.domain_map import build_domain_map
from rtl_buddy_cdc.frontend import (
    Frontend,
    elaborate_with_blackboxes,
    resolve_auto,
)
from rtl_buddy_cdc.frontends.slang import SlangFrontendUnavailable
from rtl_buddy_cdc.frontends.yosys import YosysError
from rtl_buddy_cdc.reporter import TOOL_VERSION, AnalysisResult, _instance_path
from rtl_buddy_cdc.reset_domain import (
    assign_reset_domains,
    find_reset_crossings,
    find_reset_synchronizers,
)
from rtl_buddy_cdc.reset_domain_map import build_reset_domain_map
from rtl_buddy_cdc.reset_hints import (
    ResetHints,
    ResetHintsError,
    ResetHintsUnavailable,
    load as load_reset_hints,
)
from rtl_buddy_cdc.rules import (
    Violation,
    run_all as run_all_rules,
    user_reset_polarity_overrides,
    user_reset_sync_flop_names,
)
from rtl_buddy_cdc.waivers import SuppressedViolation

app = typer.Typer(help="CDC linting tool for RTL designs.")

# Rule id for the blackbox-boundary coverage finding. Emitted by the CLI
# orchestration (not the rule pack): a blackboxed subtree the analyzer
# cannot soundly abstract — multi-clock / unresolved (left opaque) or
# reconvergence-unsafe (≥2 incoming crossings) — is an unanalysed boundary,
# i.e. a coverage gap. It fires at ``error`` severity so it fails the run by
# default; intentional opacity (a separately signed-off IP) is acknowledged
# by waiving it (``waive CDC-BBX <instance-regex>``). Per instance, so the
# cell-name regex addresses one boundary at a time.
BBOX_RULE_ID = "CDC-BBX"


class OutputFormat(str, Enum):
    text = "text"
    json = "json"
    sarif = "sarif"


class RenderFormat(str, Enum):
    mermaid = "mermaid"


_FORMAT_OPT = typer.Option(
    OutputFormat.text,
    "--format",
    "-f",
    case_sensitive=False,
    help="Report format. text (default) is human-readable; json is "
    "structured for downstream consumers; sarif targets GitHub "
    "code-scanning annotations.",
)
_OUTPUT_OPT = typer.Option(
    None,
    "--output",
    "-o",
    help="Write the report to this file (default: stdout).",
)
_SYNC_DEPTH_OPT = typer.Option(
    2,
    "--sync-depth",
    min=2,
    help="Required synchronizer depth for CDC-002. Default 2 (rule "
    "silent unless raised). Set to 3+ for high-speed / low-MTBF designs.",
)
_CLOCK_TRACE_DEPTH_OPT = typer.Option(
    16,
    "--clock-trace-depth",
    min=1,
    help="Maximum hop budget when tracing a flop's CLK net back to its "
    "top-level clock port (buffers, clock gates, muxes, divider flops "
    "each cost a hop). Default 16. Deep clock trees (long divider / "
    "buffer / ICG chains) can exceed it and leave flops domain-unknown "
    "(see the `domain_unknown` count); raise it (e.g. 40) to resolve "
    "them. Raising only ever resolves MORE flops — it never drops a "
    "crossing — so the default leaves results identical. See issue #263.",
)
_SYNC_PRIMITIVE_OPT = typer.Option(
    [],
    "--sync-primitive",
    help="Register MODULE as a sanctioned CDC synchroniser primitive: a "
    "crossing landing in an instance of it is safe by construction, the "
    "instance is summarised at its destination clock instead of being "
    "declined as a multi-clock blackbox (no CDC-BBX), and its "
    "`DEST_SYNC_FF` parameter is checked by CDC-022. Repeatable. The "
    "Xilinx XPM CDC family (xpm_cdc_single / _array_single / _gray / "
    "_handshake / _pulse / _sync_rst / _async_rst) is recognised "
    "built-in — use this only for an in-house or other-vendor macro. "
    "See issue #275.",
)
_VERBOSE_OPT = typer.Option(
    False,
    "--verbose",
    "-v",
    help="Text report only: include the per-crossing structural listing.",
)
_COLOR_OPT = typer.Option(
    None,
    "--color/--no-color",
    help="Force color on/off for the text report. Default: auto (color "
    "when stdout is a TTY and NO_COLOR is unset).",
)
_STRICT_OPT = typer.Option(
    False,
    "--strict",
    help="Promote every `warning`-severity violation to `error` before "
    "reporting. CDC-002 and CDC-005 are the rules affected today; the "
    "exit code is unchanged (any kept violation already drives exit 1) "
    "— the flag is reframing, not gating.",
)
_BASELINE_OPT = typer.Option(
    None,
    "--baseline",
    exists=True,
    readable=True,
    help="Path to a baseline JSON report (a prior `--format json` "
    "output). Violations matching baseline entries by "
    "(rule_id, cell_name, message) are filtered out of the kept set "
    "and surfaced as a 'Carried over from baseline' tally; they don't "
    "drive the exit code. Useful for 'fail PR only on new findings'.",
)
_EMIT_DOMAIN_MAP_OPT = typer.Option(
    None,
    "--emit-domain-map",
    help="Write a structured clock-domain map (JSON, schema "
    "v1.0) to this path alongside the normal report. Captures "
    "clocks, async groups, false paths, per-flop domain assignments, "
    "typed port→clock map, and structural crossings (tagged with "
    "`async_per_sdc`). Designed for downstream tools like "
    "rtl-buddy-view; see issue #106.",
)
_EMIT_RESET_DOMAIN_MAP_OPT = typer.Option(
    None,
    "--emit-reset-domain-map",
    help="Write a structured reset-domain map (JSON, schema "
    "v1.0) to this path alongside the normal report. Captures "
    "reset sources, recognised reset-synchroniser stages, per-flop "
    "reset assignments, and structural reset crossings. Parallel to "
    "`--emit-domain-map`; the two can be passed in a single run. "
    "Designed for downstream tools like rtl-buddy-view; see issue "
    "#108.",
)
_RESET_HINTS_OPT = typer.Option(
    None,
    "--reset-hints",
    exists=True,
    readable=True,
    help="YAML file declaring reset-port polarity / synchroniser "
    "annotations, parallel to the in-RTL `(* reset_polarity *)` / "
    "`(* reset_sync *)` SV attributes. Hints win on disagreement "
    "with the attribute path. Requires the `[hints]` optional "
    "extra (`pip install 'rtl-buddy-cdc[hints]'`). See issue #129 "
    "and the schema reference at "
    "wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md.",
)
_NO_FINDINGS_OPT = typer.Option(
    False,
    "--no-findings",
    help="Skip rule evaluation entirely. Only meaningful with "
    "`--emit-domain-map` or `--emit-reset-domain-map`: produces just "
    "the requested map(s) and exits 0 on successful elaboration + "
    "map emission, 2 on elaboration failure. Suppresses the normal "
    "report.",
)
_CDC_010_NO_HEURISTIC_OPT = typer.Option(
    False,
    "--cdc-010-no-heuristic",
    help="Disable CDC-010's pin-name heuristic fallback for "
    "tech-mapped cells. By default CDC-010 classifies an input pin "
    "named E / EN / CE / GATE / SE (case-insensitive) as a control "
    "pin on cell types not covered by the explicit map. Pass this "
    "flag when a library's pin naming conflicts with the heuristic "
    "(e.g. a vendor that uses `EN` for something other than enable) "
    "and you'd rather take the false negative than a false positive.",
)
_CDC_018_DEPTH_THRESHOLD_OPT = typer.Option(
    4,
    "--cdc-018-depth-threshold",
    help="Minimum sync-chain depth at which CDC-018 (cascaded "
    "synchroniser) fires. Defaults to 4 — chains of depth 2 or 3 "
    "stay silent (the textbook 2FF sync, plus a 3-stage chain "
    "common in high-MTBF designs). Raise to 5 if 4-stage chains "
    "are intentional in your design.",
    min=2,
)
_PROJECT_ROOT_OPT = typer.Option(
    None,
    "--project-root",
    help="Base directory for resolving *relative* path-bearing args — "
    "`--yosys-plugin`, `--emit-domain-map`, `--emit-reset-domain-map`. "
    "Precedence: this flag if given, else the directory of `--sdc`, else "
    "the current working directory (the legacy behaviour). Set it to a "
    "stable root (the repo, or the cdc.yaml's directory) so those args "
    "stay correct no matter where the tool is launched from — a driver "
    "that runs the tool from a deeply-nested artefact dir no longer has "
    "to hand-rebase every relative path by the cwd depth. Absolute path "
    "args are unaffected. See issue #245.",
)


def _resolution_base(project_root: Path | None, sdc_path: Path | None) -> Path:
    """Stable, absolute base for resolving relative path-bearing args.

    Precedence (see ``_PROJECT_ROOT_OPT`` / issue #245):
      1. an explicit ``--project-root``;
      2. else the directory holding ``--sdc`` (the closest thing to the
         config the tool is invoked against);
      3. else ``Path.cwd()`` (legacy).

    Always absolute so the resolved paths — and any "not found" / mkdir
    diagnostics built from them — are unambiguous regardless of the
    caller's cwd.
    """
    if project_root is not None:
        return project_root.resolve()
    if sdc_path is not None:
        return sdc_path.resolve().parent
    return Path.cwd()


def _anchor(path: Path | None, base: Path) -> Path | None:
    """Resolve a possibly-relative path arg against ``base``.

    Absolute paths pass through untouched; relative ones are joined onto
    ``base`` (the project root / SDC dir) rather than the process cwd, so
    the arg is insensitive to where the caller chose to run the tool.
    """
    if path is None:
        return None
    return path if path.is_absolute() else base / path


@app.command()
def analyze(
    netlist_path: Path = typer.Option(
        ...,
        "--netlist",
        "-n",
        exists=True,
        readable=True,
        help="Yosys write_json output (flattened netlist).",
    ),
    sdc_path: Path | None = typer.Option(
        None,
        "--sdc",
        "-s",
        exists=True,
        readable=True,
        help="SDC file. Optional, but rule checks need it.",
    ),
    waivers_path: Path | None = typer.Option(
        None,
        "--waivers",
        "-w",
        exists=True,
        readable=True,
        help="Optional waiver file (one `waive <rule|*> <regex> [reason]` "
        "per line). Suppressed violations are still reported but don't "
        "drive the exit code.",
    ),
    fmt: OutputFormat = _FORMAT_OPT,
    output_path: Path | None = _OUTPUT_OPT,
    sync_depth: int = _SYNC_DEPTH_OPT,
    sync_primitive: list[str] = _SYNC_PRIMITIVE_OPT,
    clock_trace_depth: int = _CLOCK_TRACE_DEPTH_OPT,
    verbose: bool = _VERBOSE_OPT,
    color: bool | None = _COLOR_OPT,
    strict: bool = _STRICT_OPT,
    baseline_path: Path | None = _BASELINE_OPT,
    emit_domain_map: Path | None = _EMIT_DOMAIN_MAP_OPT,
    emit_reset_domain_map: Path | None = _EMIT_RESET_DOMAIN_MAP_OPT,
    reset_hints_path: Path | None = _RESET_HINTS_OPT,
    no_findings: bool = _NO_FINDINGS_OPT,
    cdc_010_no_heuristic: bool = _CDC_010_NO_HEURISTIC_OPT,
    cdc_018_depth_threshold: int = _CDC_018_DEPTH_THRESHOLD_OPT,
    project_root: Path | None = _PROJECT_ROOT_OPT,
) -> None:
    """Analyze a flattened netlist for CDC issues (primary entry point)."""
    base = _resolution_base(project_root, sdc_path)
    emit_domain_map = _anchor(emit_domain_map, base)
    emit_reset_domain_map = _anchor(emit_reset_domain_map, base)
    code = _analyze_and_report(
        netlist_path,
        sdc_path,
        waivers_path,
        fmt,
        output_path,
        sync_depth,
        verbose=verbose,
        color=color,
        strict=strict,
        baseline_path=baseline_path,
        emit_domain_map=emit_domain_map,
        emit_reset_domain_map=emit_reset_domain_map,
        reset_hints_path=reset_hints_path,
        no_findings=no_findings,
        cdc_010_no_heuristic=cdc_010_no_heuristic,
        cdc_018_depth_threshold=cdc_018_depth_threshold,
        clock_trace_depth=clock_trace_depth,
        sync_primitives=frozenset(sync_primitive),
    )
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def lint(
    sources: list[Path] = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="Verilog/SystemVerilog source files.",
    ),
    top: str = typer.Option(..., "--top", "-t", help="Top module name."),
    sdc_path: Path | None = typer.Option(
        None,
        "--sdc",
        "-s",
        exists=True,
        readable=True,
        help="SDC file with clock declarations and async groups.",
    ),
    frontend: Frontend = typer.Option(
        Frontend.yosys,
        "--frontend",
        case_sensitive=False,
        help="Elaboration frontend. 'yosys' (default) shells out to "
        "`yosys` and runs hierarchy/proc/flatten/opt_clean before "
        "analysis. 'slang' elaborates via pyslang directly with no "
        "synth step; install the optional extra with "
        "`pip install 'rtl-buddy-cdc[slang]'`. 'auto' picks slang when "
        "pyslang is importable and falls back to yosys otherwise.",
    ),
    keep_json: Path | None = typer.Option(
        None,
        "--keep-json",
        help="Yosys frontend only: save the intermediate JSON netlist "
        "to this path (otherwise it lives in a temp file and is deleted).",
    ),
    yosys_bin: str | None = typer.Option(
        None,
        "--yosys",
        help="Yosys frontend only: path to the yosys binary "
        "(default: first `yosys` on PATH).",
    ),
    yosys_plugin: str | None = typer.Option(
        None,
        "--yosys-plugin",
        envvar="RTL_BUDDY_SLANG_PLUGIN",
        help="Yosys frontend only: path to a Yosys plugin to load before "
        "elaboration (e.g. yosys-slang's slang.so). When set, sources are "
        "read with `read_slang --std 1800-2017 --top <top>` instead of "
        "`read_verilog -sv`, giving full SystemVerilog-2017 support. "
        "Falls back to the RTL_BUDDY_SLANG_PLUGIN environment variable "
        "when the flag is omitted (the explicit flag wins); this is the "
        "machine-local .env flow rtl_buddy populates from .rtl-buddy/.env.",
    ),
    blackbox: list[str] = typer.Option(
        [],
        "--blackbox",
        help="Treat MODULE as a CDC boundary cell: keep it un-flattened "
        "(via read_slang `--blackboxed-module`) so a large subtree is "
        "analysed at its port boundary instead of being elaborated into "
        "the design. Repeatable. Requires the yosys-slang plugin "
        "(--yosys-plugin / RTL_BUDDY_SLANG_PLUGIN). The pre-elaborated "
        "`analyze` path needs no flag — a netlist already containing "
        "blackbox boundary modules loads transparently. See issue #255.",
    ),
    waivers_path: Path | None = typer.Option(
        None,
        "--waivers",
        "-w",
        exists=True,
        readable=True,
        help="Optional waiver file (see `analyze --help`).",
    ),
    fmt: OutputFormat = _FORMAT_OPT,
    output_path: Path | None = _OUTPUT_OPT,
    sync_depth: int = _SYNC_DEPTH_OPT,
    sync_primitive: list[str] = _SYNC_PRIMITIVE_OPT,
    clock_trace_depth: int = _CLOCK_TRACE_DEPTH_OPT,
    verbose: bool = _VERBOSE_OPT,
    color: bool | None = _COLOR_OPT,
    strict: bool = _STRICT_OPT,
    baseline_path: Path | None = _BASELINE_OPT,
    emit_domain_map: Path | None = _EMIT_DOMAIN_MAP_OPT,
    emit_reset_domain_map: Path | None = _EMIT_RESET_DOMAIN_MAP_OPT,
    reset_hints_path: Path | None = _RESET_HINTS_OPT,
    no_findings: bool = _NO_FINDINGS_OPT,
    cdc_010_no_heuristic: bool = _CDC_010_NO_HEURISTIC_OPT,
    cdc_018_depth_threshold: int = _CDC_018_DEPTH_THRESHOLD_OPT,
    project_root: Path | None = _PROJECT_ROOT_OPT,
) -> None:
    """Convenience wrapper: elaborate the sources using the chosen
    frontend, then analyze. With ``--frontend yosys`` (the default)
    this is equivalent to ``yosys -p 'read_verilog ...; hierarchy -top
    X; proc; flatten; opt_clean; write_json /tmp/out.json' &&
    rtl-buddy-cdc analyze --netlist /tmp/out.json --sdc ...``."""
    # Anchor the relative path-bearing args (#245) before they reach the
    # frontend / emit path, so they track the project root rather than
    # whatever cwd the caller happened to launch us in.
    base = _resolution_base(project_root, sdc_path)
    if yosys_plugin is not None:
        # ``--yosys-plugin`` is a str (it flows through to a yosys -p
        # command); anchor as a Path, then hand the frontend the resolved
        # string. An unresolvable plugin now fails with the absolute path.
        anchored_plugin = _anchor(Path(yosys_plugin), base)
        yosys_plugin = str(anchored_plugin) if anchored_plugin is not None else None
    emit_domain_map = _anchor(emit_domain_map, base)
    emit_reset_domain_map = _anchor(emit_reset_domain_map, base)

    # ``auto`` resolves once at the CLI surface so the preamble shows
    # the concrete frontend and downstream tooling parsing stdout
    # ("frontend: yosys"/"frontend: slang") doesn't see a third value.
    resolved_frontend = resolve_auto() if frontend is Frontend.auto else frontend

    if fmt is OutputFormat.text:
        # Preamble for human readers only — structured output mustn't
        # carry non-payload framing.
        if frontend is Frontend.auto:
            typer.echo(f"frontend: {resolved_frontend.value} (auto)")
        else:
            typer.echo(f"frontend: {resolved_frontend.value}")
        typer.echo(f"top:      {top}")
        for s in sources:
            typer.echo(f"src:      {s}")

    try:
        # ``_with_blackboxes`` so the lint path auto-abstracts the same
        # way ``analyze`` does (#257): a ``--blackbox`` subtree arrives as
        # a sibling module the shared analysis core summarises, instead of
        # being silently dropped by the single-return ``elaborate``.
        module, blackboxes = elaborate_with_blackboxes(
            sources,
            top,
            resolved_frontend,
            yosys_bin=yosys_bin,
            keep_json=keep_json,
            yosys_plugin=yosys_plugin,
            blackbox=list(blackbox),
        )
    except SlangFrontendUnavailable as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2)
    except YosysError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2)
    except NotImplementedError as e:
        # Stub frontend path (slang today). Distinct exit signal from
        # a missing-binary failure so CI scripts can tell them apart.
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2)

    if (
        fmt is OutputFormat.text
        and resolved_frontend is Frontend.yosys
        and keep_json is not None
    ):
        typer.echo(f"netlist JSON kept at: {keep_json}")

    code = _analyze_module_and_report(
        module,
        sdc_path,
        waivers_path,
        fmt,
        output_path,
        sync_depth,
        verbose=verbose,
        color=color,
        strict=strict,
        baseline_path=baseline_path,
        emit_domain_map=emit_domain_map,
        emit_reset_domain_map=emit_reset_domain_map,
        reset_hints_path=reset_hints_path,
        no_findings=no_findings,
        cdc_010_no_heuristic=cdc_010_no_heuristic,
        cdc_018_depth_threshold=cdc_018_depth_threshold,
        clock_trace_depth=clock_trace_depth,
        blackboxes=blackboxes or None,
        sync_primitives=frozenset(sync_primitive),
    )
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def render(
    map_path: Path = typer.Option(
        ...,
        "--map",
        "-m",
        exists=True,
        readable=True,
        help="Path to a v1.0 domain map JSON (as produced by "
        "`analyze --emit-domain-map`). The renderer is a pure "
        "transformation over the existing artifact and does not re-run "
        "the analyzer.",
    ),
    fmt: RenderFormat = typer.Option(
        RenderFormat.mermaid,
        "--format",
        "-f",
        case_sensitive=False,
        help="Output format. Currently only `mermaid` (GitHub-renderable "
        "fenced block). Additional formats may follow — see issue #162.",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the diagram to this file (default: stdout).",
    ),
) -> None:
    """Render a domain map as a diagram (mermaid for now).

    The output groups flops into one subgraph per clock domain, draws
    async-per-SDC crossings as dashed warning edges, and surfaces
    top-level ports as stadium nodes anchored to their declared clock.
    Designed for fixture-level documentation that renders inline on
    GitHub.
    """
    try:
        map_data = json.loads(map_path.read_text())
    except json.JSONDecodeError as exc:
        typer.echo(f"error: {map_path}: not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Only one format today; the enum exists so additional formats
    # plug in with no flag-surface churn (see issue #162).
    renderers = {RenderFormat.mermaid: render_mod.render_mermaid}
    try:
        out = renderers[fmt](map_data)
    except render_mod.RenderError as exc:
        typer.echo(f"error: {map_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if output_path is None:
        typer.echo(out, nl=False)
    else:
        output_path.write_text(out)


@app.command()
def version() -> None:
    """Print tool, yosys, and (when installed) pyslang versions."""
    typer.echo(f"rtl-buddy-cdc {TOOL_VERSION}")
    yosys = shutil.which("yosys")
    if yosys:
        out = subprocess.run(
            [yosys, "-V"], capture_output=True, text=True
        ).stdout.strip()
        typer.echo(out)
    else:
        typer.echo("yosys: not found on PATH")

    # pyslang is the slang frontend's optional runtime dep. We probe via
    # importlib.metadata (not the lazy import in frontends/slang.py)
    # because we want the wheel version, not the slang C++ build the
    # wheel wraps — bug-report diagnostics key on the former.
    import importlib.metadata as _md

    try:
        typer.echo(f"pyslang: {_md.version('pyslang')}")
    except _md.PackageNotFoundError:
        typer.echo("pyslang: not installed (optional; install with the [slang] extra)")


# --- shared analysis path ---------------------------------------------------


def _analyze_and_report(
    netlist_path: Path,
    sdc_path: Path | None,
    waivers_path: Path | None,
    fmt: OutputFormat,
    output_path: Path | None,
    sync_depth: int = 2,
    *,
    verbose: bool = False,
    color: bool | None = None,
    strict: bool = False,
    baseline_path: Path | None = None,
    emit_domain_map: Path | None = None,
    emit_reset_domain_map: Path | None = None,
    reset_hints_path: Path | None = None,
    no_findings: bool = False,
    cdc_010_no_heuristic: bool = False,
    cdc_018_depth_threshold: int = 4,
    clock_trace_depth: int = 16,
    sync_primitives: frozenset[str] = frozenset(),
) -> int:
    """Load a Yosys JSON netlist and run the shared analyze+report path."""
    module, blackboxes = netlist.load_with_blackboxes(netlist_path)
    return _analyze_module_and_report(
        module,
        sdc_path,
        waivers_path,
        fmt,
        output_path,
        sync_depth,
        verbose=verbose,
        color=color,
        strict=strict,
        baseline_path=baseline_path,
        emit_domain_map=emit_domain_map,
        emit_reset_domain_map=emit_reset_domain_map,
        reset_hints_path=reset_hints_path,
        no_findings=no_findings,
        cdc_010_no_heuristic=cdc_010_no_heuristic,
        cdc_018_depth_threshold=cdc_018_depth_threshold,
        clock_trace_depth=clock_trace_depth,
        blackboxes=blackboxes,
        sync_primitives=sync_primitives,
    )


def _analyze_module_and_report(
    module: netlist.Module,
    sdc_path: Path | None,
    waivers_path: Path | None,
    fmt: OutputFormat,
    output_path: Path | None,
    sync_depth: int = 2,
    *,
    verbose: bool = False,
    color: bool | None = None,
    strict: bool = False,
    baseline_path: Path | None = None,
    emit_domain_map: Path | None = None,
    emit_reset_domain_map: Path | None = None,
    reset_hints_path: Path | None = None,
    no_findings: bool = False,
    cdc_010_no_heuristic: bool = False,
    cdc_018_depth_threshold: int = 4,
    clock_trace_depth: int = 16,
    blackboxes: dict[str, netlist.Module] | None = None,
    sync_primitives: frozenset[str] = frozenset(),
) -> int:
    """Run the analyzer on an in-memory ``Module`` and dispatch to the
    chosen reporter. Returns a process-style exit code: 0 = clean (or
    no SDC so rule checks were skipped, or ``--no-findings``), 1 = at
    least one *unsuppressed* rule violation. Waived findings (and
    baseline-carried findings) are still reported but don't fail the
    run.

    ``--no-findings`` is only meaningful alongside ``--emit-domain-map``:
    rule evaluation is skipped, the normal report is suppressed, and
    the exit code is 0 on successful map emission (elaboration failure
    still exits 2 from the lint wrapper before we get here)."""
    # Load --reset-hints early so an unreadable / malformed file
    # fails the run before any analysis cost — same pattern the SDC
    # loader uses. Loud failure with file:line context; see #129.
    reset_hints: ResetHints | None = None
    if reset_hints_path is not None:
        try:
            reset_hints = load_reset_hints(reset_hints_path)
        except ResetHintsUnavailable as e:
            typer.echo(f"error: {e}", err=True)
            return 2
        except ResetHintsError as e:
            typer.echo(f"error: {e}", err=True)
            return 2

    spec: sdc_mod.ClockSpec | None = None
    async_crossings: list[Crossing] = []
    violations: list[Violation] = []
    if sdc_path is not None:
        spec = sdc_mod.parse_file(sdc_path)
        # Synthesize sentinel entries for input ports the SDC didn't
        # type via ``set_input_delay -clock``. Lets find_crossings's
        # existing port-walk emit port-sourced crossings for them so
        # CDC-011 can fire — see ``sdc.synthesize_unconstrained_inputs``.
        sdc_mod.synthesize_unconstrained_inputs(spec, module)
        # G-11 (rtl-buddy-cdc#218): cross-statement clock-graph
        # diagnostics — same-port-in-multiple-clocks, unresolved
        # master, master cycles. Merged into partial_warnings so
        # they flow through the existing CLI warning surface.
        spec.partial_warnings.extend(sdc_mod.validate_clock_graph(spec))
        if spec.partial_warnings and fmt is OutputFormat.text and not no_findings:
            for w in spec.partial_warnings:
                typer.echo(f"warning: {sdc_path}: {w}", err=True)
        domains = assign_domains(
            module,
            pin_clocks=spec.pin_clocks,
            clock_for_port=spec.clock_for_port,
            max_depth=clock_trace_depth,
        )
        # Auto-abstract single-clock subtrees (#256/#257): summarise each
        # blackbox sibling whose whole clock set sits in one async-safe
        # domain to its port boundary, keyed per instance so the same
        # module under two domains is summarised correctly while identical
        # instances hit the cache. ``find_crossings`` then re-seeds each
        # subtree's boundary crossings (output-side virtual sources AND
        # input-side virtual sinks) without walking the (absent) internal
        # flops. Non-single-clock siblings get no summary and are simply
        # not re-seeded. The same sequence runs for the lint path, which
        # passes the blackbox siblings the frontend produced.
        boundaries, comp_stats = compose_boundaries(
            module,
            blackboxes,
            spec,
            max_depth=clock_trace_depth,
            sync_primitives=sync_primitives,
        )
        crossings = find_crossings(
            module,
            port_clock=spec.port_clock,
            pin_clocks=spec.pin_clocks,
            clock_for_port=spec.clock_for_port,
            boundaries=boundaries,
            max_depth=clock_trace_depth,
        )
        # FIX 3 (reconvergence gate): a single-clock block that IS
        # abstracted but has >=2 distinct foreign-domain crossings entering
        # DISTINCT input ports can hide an internal reconvergence (CDC-005)
        # the flat design would flag. The boundary star-collapse severs the
        # subtree's internal graph, so that reconvergence cannot be checked
        # at the boundary. Conservative policy: REFUSE to abstract such a
        # block — drop it from ``boundaries`` (so it becomes opaque, no
        # boundary crossings emitted for it) and RE-RUN find_crossings.
        unsafe = reconvergence_unsafe_instances(crossings)
        # Coverage findings for blackboxes the analyzer cannot soundly
        # abstract — emitted as ``error`` Violations (waivable) so an
        # unanalysed boundary fails the run rather than passing with only a
        # warning. Folded into ``violations`` after the rule pass below.
        bb_violations: list[Violation] = []
        if unsafe:
            reconv_ports = {
                inst: sorted(
                    {
                        c.dst_boundary[1]
                        for c in crossings
                        if c.dst_boundary is not None and c.dst_boundary[0] == inst
                    }
                )
                for inst in unsafe
            }
            for inst in sorted(unsafe):
                bb_violations.append(
                    Violation(
                        rule_id=BBOX_RULE_ID,
                        severity="error",
                        message=(
                            f"blackbox `{inst}` (`{module.cells[inst].type}`) has "
                            f"crossings into {len(reconv_ports[inst])} input ports; "
                            f"reconvergence among them cannot be checked at the "
                            f"boundary — flatten it or analyse standalone "
                            f"(waive {BBOX_RULE_ID} if intentionally not checked here)."
                        ),
                        cell_name=inst,
                    )
                )
            boundaries = {k: v for k, v in boundaries.items() if k not in unsafe}
            comp_stats = dataclasses.replace(
                comp_stats,
                boundary_modules=frozenset(module.cells[k].type for k in boundaries),
            )
            crossings = find_crossings(
                module,
                port_clock=spec.port_clock,
                pin_clocks=spec.pin_clocks,
                clock_for_port=spec.clock_for_port,
                boundaries=boundaries,
                max_depth=clock_trace_depth,
            )
        # FIX 2 (now an error): every declined/opaque blackbox (multi-clock /
        # unresolved per FIX 1) is a coverage gap — its zero-cell internals
        # are unanalysed and it is absent from ``boundaries``. Emit one
        # ``error`` per instance so it is waivable by cell name and carries a
        # source location, instead of a silent drop.
        for inst, cell in module.cells.items():
            if cell.type in comp_stats.declined_modules:
                bb_violations.append(
                    Violation(
                        rule_id=BBOX_RULE_ID,
                        severity="error",
                        message=(
                            f"blackbox `{inst}` (`{cell.type}`) left opaque — not "
                            f"provably single-clock; internal crossings not analysed. "
                            f"Flatten it or analyse standalone "
                            f"(waive {BBOX_RULE_ID} if intentionally not checked here)."
                        ),
                        cell_name=inst,
                    )
                )
        # FIX 4: per-instance clock-pin map for CDC-008's clock-as-data
        # exemption — exempt only a blackbox instance's CLOCK pins, not
        # the whole cell, so a clock wired into a genuine DATA input of a
        # blackbox still fires.
        boundary_clock_pins: dict[str, frozenset[str]] = {}
        if blackboxes:
            for inst_name, cell in module.cells.items():
                sub = blackboxes.get(cell.type)
                if sub is None:
                    continue
                boundary_clock_pins[inst_name] = instance_clock_pins(
                    module,
                    cell,
                    sub,
                    spec=spec,
                    pin_clocks=spec.pin_clocks,
                    max_depth=clock_trace_depth,
                )
        async_crossings = _filter_async(crossings, spec)
        if not no_findings:
            violations = run_all_rules(
                module,
                async_crossings,
                spec,
                required_depth=sync_depth,
                reset_hints=reset_hints,
                cdc_010_heuristic=not cdc_010_no_heuristic,
                cdc_018_depth_threshold=cdc_018_depth_threshold,
                boundary_modules=comp_stats.boundary_modules,
                blackbox_modules=frozenset(blackboxes or {}),
                boundary_clock_pins=boundary_clock_pins,
                max_depth=clock_trace_depth,
                sync_primitives=sync_primitives,
            )
            # Blackbox-boundary coverage findings lead the list so an
            # unanalysed boundary is the first thing reported; they are
            # ordinary ``error`` Violations from here on (waivable,
            # exit-code-driving).
            violations = bb_violations + violations
    else:
        domains = assign_domains(module, max_depth=clock_trace_depth)
        crossings = find_crossings(module, max_depth=clock_trace_depth)

    suppressed: list[SuppressedViolation] = []
    if waivers_path is not None:
        waivers = waivers_mod.parse_file(waivers_path)
        violations, suppressed = waivers_mod.apply(violations, waivers)

    # --baseline filter: partition findings against a prior JSON report.
    # Carryover entries stay in the report (separate tally) but never
    # drive the exit code — auto-derived waivers, basically.
    baseline_carryover: list[Violation] = []
    if baseline_path is not None:
        baseline_keys = _load_baseline_keys(baseline_path)
        kept: list[Violation] = []
        for v in violations:
            if _violation_key(v) in baseline_keys:
                baseline_carryover.append(v)
            else:
                kept.append(v)
        violations = kept

    # --strict: promote every kept ``warning`` to ``error`` before the
    # reporter sees the list. Suppressed and baseline-carried findings
    # are left alone — by definition they aren't driving exit-code
    # outcomes that the user is asking to tighten.
    if strict:
        violations = [
            dataclasses.replace(v, severity="error") if v.severity == "warning" else v
            for v in violations
        ]

    # Resolve each violation's hierarchical instance path from its
    # ``cell_name``. Done here, at the CLI boundary, so the rule pack
    # stays frontend-agnostic (no ``reporter`` import in ``rules.py``).
    # See ``wiki/raw/articles/hierarchical-reporting.md`` for the
    # resolver contract and the parent issue #46 for the rendering
    # phases that consume the field.
    violations = [
        dataclasses.replace(v, instance_path=_instance_path(module, v.cell_name))
        for v in violations
    ]
    suppressed = [
        dataclasses.replace(
            s,
            violation=dataclasses.replace(
                s.violation,
                instance_path=_instance_path(module, s.violation.cell_name),
            ),
        )
        for s in suppressed
    ]
    baseline_carryover = [
        dataclasses.replace(v, instance_path=_instance_path(module, v.cell_name))
        for v in baseline_carryover
    ]

    # Advisory inferred-clock candidates (P3/#263): internal nets that
    # fan out to many flop CLK pins but carry no declared clock identity.
    # Computed from the netlist + SDC pin map only — it never reads
    # ``domains`` / ``crossings`` and never feeds back into them, so it
    # cannot change any classification. Honour ``pin_clocks`` so a net the
    # user already declared with ``create_generated_clock`` is not
    # re-flagged.
    inferred_clock_candidates = find_inferred_clock_candidates(
        module, pin_clocks=spec.pin_clocks if spec is not None else None
    )

    result = AnalysisResult(
        module=module,
        domains=domains,
        crossings=crossings,
        async_crossings=async_crossings,
        spec=spec,
        violations=list(violations),
        suppressed=list(suppressed),
        baseline_carryover=list(baseline_carryover),
        inferred_clock_candidates=inferred_clock_candidates,
    )

    # Domain-map emission runs *before* the normal report so a write
    # failure surfaces deterministically as an OSError rather than being
    # masked by the report's own I/O path.
    if emit_domain_map is not None:
        clock_net_crossings = find_clock_network_crossings(
            module,
            spec,
            clock_for_port=spec.clock_for_port if spec is not None else None,
            use_heuristic=not cdc_010_no_heuristic,
            max_depth=clock_trace_depth,
        )
        payload = build_domain_map(
            module,
            domains,
            crossings,
            spec,
            async_crossings=async_crossings,
            clock_network_crossings=clock_net_crossings,
        )
        # Create the parent dir (#245): an emit target under an
        # uncommitted dir like ``.rtl-buddy/overlays/`` would otherwise
        # raise FileNotFoundError on a fresh checkout.
        emit_domain_map.parent.mkdir(parents=True, exist_ok=True)
        with emit_domain_map.open("w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")

    if emit_reset_domain_map is not None:
        flop_clocks = {fd.flop.cell.name: fd.clock for fd in domains}
        reset_domains_map = assign_reset_domains(module)
        polarity_overrides = user_reset_polarity_overrides(module, hints=reset_hints)
        recognised_syncs = find_reset_synchronizers(
            module,
            flop_clocks,
            extra_synchronizers=user_reset_sync_flop_names(module, hints=reset_hints),
        )
        reset_crossings = find_reset_crossings(
            module,
            flop_clocks,
            recognised_syncs=recognised_syncs,
            polarity_overrides=polarity_overrides,
        )
        reset_payload = build_reset_domain_map(
            module,
            reset_domains_map,
            flop_clocks,
            recognised_syncs,
            polarity_overrides,
            reset_crossings,
        )
        emit_reset_domain_map.parent.mkdir(parents=True, exist_ok=True)
        with emit_reset_domain_map.open("w") as fh:
            json.dump(reset_payload, fh, indent=2)
            fh.write("\n")

    # ``--no-findings`` suppresses the normal report entirely (the run
    # exists to produce the map). Exit 0 — there were no findings to
    # drive a non-zero code.
    if no_findings:
        return 0

    out: IO[str]
    close_after = False
    if output_path is None:
        out = sys.stdout
    else:
        out = output_path.open("w")
        close_after = True
    try:
        if fmt is OutputFormat.text:
            # If we're writing to a file, force color off — ANSI in a
            # text file looks like garbage. Otherwise honor the
            # explicit flag (None → auto-detect against stdout).
            text_color = False if output_path is not None else color
            reporter.render_text(result, out, verbose=verbose, color=text_color)
        elif fmt is OutputFormat.json:
            reporter.render_json(result, out)
        elif fmt is OutputFormat.sarif:
            reporter.render_sarif(result, out)
    finally:
        if close_after:
            out.close()

    return 1 if violations else 0


# --- --baseline support ------------------------------------------------------
#
# The match key is keyed on JSON-contract fields exposed by
# ``reporter._violation_to_dict``: ``rule_id``, ``cell_name`` (added
# alongside this feature so each finding has a stable per-cell handle),
# and ``message``. Two genuinely-identical findings collapse to one key;
# that's the same coarseness waivers already accept.


def _violation_key(v: Violation) -> tuple[str, str, str]:
    return (v.rule_id, v.cell_name or "", v.message)


def _load_baseline_keys(path: Path) -> set[tuple[str, str, str]]:
    """Parse a baseline JSON report and return the set of match keys.

    Reads both ``violations`` and ``baseline_carryover`` so chained
    baselines stay stable: a finding that was carried over in the
    baseline run is still treated as carried over here.
    """
    with path.open() as fh:
        payload = json.load(fh)
    keys: set[tuple[str, str, str]] = set()
    for bucket in ("violations", "baseline_carryover"):
        for entry in payload.get(bucket, []):
            keys.add(
                (
                    entry.get("rule_id", ""),
                    entry.get("cell_name") or "",
                    entry.get("message", ""),
                )
            )
    return keys


def _summarise_blackboxes(
    module: netlist.Module,
    blackboxes: dict[str, netlist.Module] | None,
    spec: "sdc_mod.ClockSpec",
    *,
    max_depth: int = 16,
) -> dict[str, netlist.BoundarySummary]:
    """Auto-abstract single-clock blackbox subtrees to port boundaries.

    Thin CLI-side wrapper over the compositional walk
    (:func:`rtl_buddy_cdc.hierarchy.compose_boundaries`): each distinct
    ``(module type, clock context)`` is summarised once (cached by that
    pair) so identical instances are analysed once, while an instance in
    a different domain gets its own correct summary (#257). The returned
    map is keyed **per instance** (cell name) so ``find_crossings`` can
    re-seed each instance's boundary crossings against the domain its
    parent actually drives.

    Kept as a named entry point because the test-suite imports it
    directly; the :class:`~rtl_buddy_cdc.hierarchy.CompositionStats` half
    of the compose result is dropped here (callers that want the cache
    accounting or the CDC-008 boundary-module set call
    ``compose_boundaries`` directly).
    """
    boundaries, _stats = compose_boundaries(
        module, blackboxes, spec, max_depth=max_depth
    )
    return boundaries


def _filter_async(
    crossings: list[Crossing], spec: "sdc_mod.ClockSpec"
) -> list[Crossing]:
    """Keep only crossings the rule pack should see.

    A crossing is kept iff:
      - its endpoints resolve to different clocks (so generated
        clocks fold back into their masters before comparison), and
      - the resolved roots aren't in different exclusive groups
        (logically/physically-exclusive clocks never coexist at
        runtime, so the path is unreachable), and
      - the resolved roots are declared asynchronous via
        ``set_clock_groups -asynchronous`` or ``set_false_path
        -from/-to``.
    """
    out: list[Crossing] = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            out.append(c)
    return out
