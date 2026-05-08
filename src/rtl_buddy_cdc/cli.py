from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.domain import assign_domains, find_crossings

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
        help="SDC file. Optional for now; required once rule checks land.",
    ),
) -> None:
    """Analyze a flattened netlist for CDC issues (MVP: list crossings)."""
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

    if sdc_path is not None:
        typer.echo(f"  sdc: {sdc_path} (parser not yet implemented)")


@app.command()
def lint(
    sources: list[Path] = typer.Argument(
        ..., help="Verilog/SystemVerilog source files."
    ),
    top: str = typer.Option(..., "--top", "-t", help="Top module name."),
) -> None:
    """Run CDC lint on the given design (stub: no yosys invocation yet)."""
    yosys = shutil.which("yosys")
    if yosys is None:
        raise typer.Exit("yosys not found on PATH")
    typer.echo(f"yosys: {yosys}")
    typer.echo(f"top:   {top}")
    for s in sources:
        typer.echo(f"src:   {s}")
    typer.echo("[stub] CDC analysis not yet implemented.")


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
