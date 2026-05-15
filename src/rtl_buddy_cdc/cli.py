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

from rtl_buddy_cdc import netlist, reporter, sdc as sdc_mod, waivers as waivers_mod
from rtl_buddy_cdc.domain import Crossing, assign_domains, find_crossings
from rtl_buddy_cdc.frontend import Frontend, elaborate, resolve_auto
from rtl_buddy_cdc.frontends.slang import SlangFrontendUnavailable
from rtl_buddy_cdc.frontends.yosys import YosysError
from rtl_buddy_cdc.reporter import AnalysisResult
from rtl_buddy_cdc.rules import Violation, run_all as run_all_rules
from rtl_buddy_cdc.waivers import SuppressedViolation

app = typer.Typer(help="CDC linting tool for RTL designs.")


class OutputFormat(str, Enum):
    text = "text"
    json = "json"
    sarif = "sarif"


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
    verbose: bool = _VERBOSE_OPT,
    color: bool | None = _COLOR_OPT,
    strict: bool = _STRICT_OPT,
    baseline_path: Path | None = _BASELINE_OPT,
) -> None:
    """Analyze a flattened netlist for CDC issues (primary entry point)."""
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
        help="Yosys frontend only: path to a Yosys plugin to load before "
        "elaboration (e.g. yosys-slang's slang.so). When set, sources are "
        "read with `read_slang --std 1800-2017 --top <top>` instead of "
        "`read_verilog -sv`, giving full SystemVerilog-2017 support.",
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
    verbose: bool = _VERBOSE_OPT,
    color: bool | None = _COLOR_OPT,
    strict: bool = _STRICT_OPT,
    baseline_path: Path | None = _BASELINE_OPT,
) -> None:
    """Convenience wrapper: elaborate the sources using the chosen
    frontend, then analyze. With ``--frontend yosys`` (the default)
    this is equivalent to ``yosys -p 'read_verilog ...; hierarchy -top
    X; proc; flatten; opt_clean; write_json /tmp/out.json' &&
    rtl-buddy-cdc analyze --netlist /tmp/out.json --sdc ...``."""
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
        module = elaborate(
            sources,
            top,
            frontend=resolved_frontend,
            yosys_bin=yosys_bin,
            keep_json=keep_json,
            yosys_plugin=yosys_plugin,
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
    )
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def version() -> None:
    """Print tool, yosys, and (when installed) pyslang versions."""
    typer.echo("rtl-buddy-cdc 0.1.0")
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
) -> int:
    """Load a Yosys JSON netlist and run the shared analyze+report path."""
    module = netlist.load(netlist_path)
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
) -> int:
    """Run the analyzer on an in-memory ``Module`` and dispatch to the
    chosen reporter. Returns a process-style exit code: 0 = clean (or
    no SDC so rule checks were skipped), 1 = at least one *unsuppressed*
    rule violation. Waived findings (and baseline-carried findings) are
    still reported but don't fail the run."""
    spec: sdc_mod.ClockSpec | None = None
    async_crossings: list[Crossing] = []
    violations: list[Violation] = []
    if sdc_path is not None:
        spec = sdc_mod.parse_file(sdc_path)
        if spec.partial_warnings and fmt is OutputFormat.text:
            for w in spec.partial_warnings:
                typer.echo(f"warning: {sdc_path}: {w}", err=True)
        domains = assign_domains(module, pin_clocks=spec.pin_clocks)
        crossings = find_crossings(
            module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
        )
        async_crossings = _filter_async(crossings, spec)
        violations = run_all_rules(
            module, async_crossings, spec, required_depth=sync_depth
        )
    else:
        domains = assign_domains(module)
        crossings = find_crossings(module)

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

    result = AnalysisResult(
        module=module,
        domains=domains,
        crossings=crossings,
        async_crossings=async_crossings,
        spec=spec,
        violations=list(violations),
        suppressed=list(suppressed),
        baseline_carryover=list(baseline_carryover),
    )

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
