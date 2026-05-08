from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import Crossing, assign_domains, find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

app = typer.Typer(help="CDC linting tool for RTL designs (Yosys-backed).")


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
) -> None:
    """Analyze a flattened netlist for CDC issues (primary entry point)."""
    code = _analyze_and_report(netlist_path, sdc_path)
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

        code = _analyze_and_report(tmp_json, sdc_path)
        if code != 0:
            raise typer.Exit(code=code)
    finally:
        if keep_json is not None:
            try:
                shutil.copy(tmp_json, keep_json)
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


def _analyze_and_report(netlist_path: Path, sdc_path: Path | None) -> int:
    """Run the analyzer on a netlist JSON. Prints the report; returns
    a process-style exit code (0 = clean / informational, 1 = at least
    one rule violation)."""
    module = netlist.load(netlist_path)
    typer.echo(f"module: {module.name}")
    typer.echo(f"  ports: {len(module.ports)}")
    typer.echo(f"  cells: {len(module.cells)}")

    domains = assign_domains(module)
    typer.echo(f"  flops: {len(domains)}")
    by_clock: dict[str | None, int] = {}
    for fd in domains:
        by_clock[fd.clock] = by_clock.get(fd.clock, 0) + 1
    for clk, n in sorted(by_clock.items(), key=lambda x: (x[0] is None, x[0] or "")):
        label = clk if clk is not None else "<unresolved>"
        typer.echo(f"    domain {label}: {n} flop(s)")

    crossings = find_crossings(module)
    typer.echo(f"  crossings (flop→flop, different clock): {len(crossings)}")
    for c in crossings:
        typer.echo(
            f"    {c.src_clock} → {c.dst_clock}  "
            f"({c.src_flop.name} → {c.dst_flop.name}, "
            f"width={c.width}, min_hops={c.min_hops})"
        )

    if sdc_path is None:
        typer.echo(
            "  no SDC supplied — skipping rule checks "
            "(every cross-clock crossing is treated as synchronous)"
        )
        return 0

    spec = sdc_mod.parse_file(sdc_path)
    typer.echo(
        f"  sdc: {len(spec.clocks)} clock(s), "
        f"{len(spec.async_groups)} async-group statement(s)"
    )

    async_crossings = _filter_async(crossings, spec)
    typer.echo(f"  async crossings (per SDC clock groups): {len(async_crossings)}")

    violations = run_all_rules(module, async_crossings, spec)
    if not violations:
        typer.echo("  no rule violations.")
        return 0

    typer.echo(f"  {len(violations)} violation(s):")
    for v in violations:
        typer.echo(f"    [{v.rule_id}] {v.severity}: {v.message}")
    return 1


def _filter_async(
    crossings: list[Crossing], spec: "sdc_mod.ClockSpec"
) -> list[Crossing]:
    """Keep only crossings whose endpoints are in different async groups.

    The crossing's ``src_clock`` / ``dst_clock`` here are *port names*
    (per :func:`assign_domains`); we map each to its SDC clock name
    before consulting the async-group table.
    """
    out: list[Crossing] = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.are_async(a, b):
            out.append(c)
    return out
