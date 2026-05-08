from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import IO

import typer

from rtl_buddy_cdc import netlist, reporter, sdc as sdc_mod, waivers as waivers_mod
from rtl_buddy_cdc.domain import Crossing, assign_domains, find_crossings
from rtl_buddy_cdc.reporter import AnalysisResult
from rtl_buddy_cdc.rules import run_all as run_all_rules

app = typer.Typer(help="CDC linting tool for RTL designs (Yosys-backed).")


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
) -> None:
    """Analyze a flattened netlist for CDC issues (primary entry point)."""
    code = _analyze_and_report(netlist_path, sdc_path, waivers_path, fmt, output_path)
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
    keep_json: Path | None = typer.Option(
        None,
        "--keep-json",
        help="Save the intermediate Yosys JSON netlist to this path "
        "(otherwise it lives in a temp file and is deleted).",
    ),
    yosys_bin: str | None = typer.Option(
        None,
        "--yosys",
        help="Path to the yosys binary (default: first `yosys` on PATH).",
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
) -> None:
    """Convenience wrapper: run yosys to produce a flattened netlist,
    then analyze it. Equivalent to ``yosys -p 'read_verilog ...; \
hierarchy -top X; proc; flatten; opt_clean; write_json /tmp/out.json' \
&& rtl-buddy-cdc analyze --netlist /tmp/out.json --sdc ...``."""
    yosys = yosys_bin or shutil.which("yosys")
    if yosys is None or not Path(yosys).exists():
        typer.echo("error: yosys not found on PATH (use --yosys to override)", err=True)
        raise typer.Exit(code=2)

    tmp_json = Path(tempfile.mkstemp(suffix=".json", prefix="rtl-buddy-cdc-")[1])
    try:
        srcs = " ".join(shlex.quote(str(s)) for s in sources)
        script = (
            f"read_verilog -sv {srcs}; "
            f"hierarchy -top {shlex.quote(top)}; "
            f"proc; flatten; opt_clean; "
            f"write_json {shlex.quote(str(tmp_json))}"
        )
        if fmt is OutputFormat.text:
            # Only print preamble in text mode — structured output
            # mustn't contain non-payload preamble.
            typer.echo(f"yosys: {yosys}")
            typer.echo(f"top:   {top}")
            for s in sources:
                typer.echo(f"src:   {s}")

        proc = subprocess.run(
            [yosys, "-q", "-p", script],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            typer.echo("yosys elaboration failed:", err=True)
            if proc.stderr:
                typer.echo(proc.stderr.rstrip(), err=True)
            if proc.stdout:
                typer.echo(proc.stdout.rstrip(), err=True)
            raise typer.Exit(code=2)

        code = _analyze_and_report(tmp_json, sdc_path, waivers_path, fmt, output_path)
        if code != 0:
            raise typer.Exit(code=code)
    finally:
        if keep_json is not None:
            try:
                shutil.copy(tmp_json, keep_json)
                if fmt is OutputFormat.text:
                    typer.echo(f"netlist JSON kept at: {keep_json}")
            except OSError as e:
                typer.echo(f"warning: could not save --keep-json: {e}", err=True)
        try:
            tmp_json.unlink()
        except FileNotFoundError:
            pass


@app.command()
def version() -> None:
    """Print yosys and tool versions."""
    typer.echo("rtl-buddy-cdc 0.1.0")
    yosys = shutil.which("yosys")
    if yosys:
        out = subprocess.run(
            [yosys, "-V"], capture_output=True, text=True
        ).stdout.strip()
        typer.echo(out)
    else:
        typer.echo("yosys: not found on PATH")


# --- shared analysis path ---------------------------------------------------


def _analyze_and_report(
    netlist_path: Path,
    sdc_path: Path | None,
    waivers_path: Path | None,
    fmt: OutputFormat,
    output_path: Path | None,
) -> int:
    """Run the analyzer on a netlist JSON and dispatch to the chosen
    reporter. Returns a process-style exit code: 0 = clean (or no SDC
    so rule checks were skipped), 1 = at least one *unsuppressed* rule
    violation. Waived findings are still reported but don't fail the
    run."""
    module = netlist.load(netlist_path)
    domains = assign_domains(module)
    crossings = find_crossings(module)

    spec: sdc_mod.ClockSpec | None = None
    async_crossings: list[Crossing] = []
    violations = []
    if sdc_path is not None:
        spec = sdc_mod.parse_file(sdc_path)
        async_crossings = _filter_async(crossings, spec)
        violations = run_all_rules(module, async_crossings, spec)

    suppressed = []
    if waivers_path is not None:
        waivers = waivers_mod.parse_file(waivers_path)
        violations, suppressed = waivers_mod.apply(violations, waivers)

    result = AnalysisResult(
        module=module,
        domains=domains,
        crossings=crossings,
        async_crossings=async_crossings,
        spec=spec,
        violations=list(violations),
        suppressed=list(suppressed),
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
            reporter.render_text(result, out)
        elif fmt is OutputFormat.json:
            reporter.render_json(result, out)
        elif fmt is OutputFormat.sarif:
            reporter.render_sarif(result, out)
    finally:
        if close_after:
            out.close()

    return 1 if violations else 0


def _filter_async(
    crossings: list[Crossing], spec: "sdc_mod.ClockSpec"
) -> list[Crossing]:
    """Keep only crossings whose endpoints are in different async groups."""
    out: list[Crossing] = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.are_async(a, b):
            out.append(c)
    return out
