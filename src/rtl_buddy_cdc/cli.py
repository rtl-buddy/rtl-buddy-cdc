from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

app = typer.Typer(help="CDC linting tool for RTL designs (Yosys-backed).")


@app.command()
def lint(
    sources: list[Path] = typer.Argument(..., help="Verilog/SystemVerilog source files."),
    top: str = typer.Option(..., "--top", "-t", help="Top module name."),
) -> None:
    """Run CDC lint on the given design (stub)."""
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
        out = subprocess.run([yosys, "-V"], capture_output=True, text=True).stdout.strip()
        typer.echo(out)
    else:
        typer.echo("yosys: not found on PATH")
