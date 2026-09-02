"""``lint --incdir`` reaches both frontends.

A design whose header lives in a directory only reachable through an
include path (the shape a filelist ``+incdir+`` produces) must elaborate
with ``--incdir`` and fail without it, under the yosys frontend and the
pyslang one alike. Each frontend's tests skip when its toolchain is
missing.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

YOSYS = shutil.which("yosys")
PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
runner = CliRunner()


def _design(root: Path) -> tuple[Path, Path, Path]:
    """Two-clock design with its width in a header under ``inc/``.
    Returns ``(source, incdir, sdc)``."""
    inc = root / "inc"
    inc.mkdir()
    (inc / "width.svh").write_text("`define W 4\n")
    src = root / "top.sv"
    src.write_text(
        '`include "width.svh"\n'
        "module top(input clk_a, input clk_b, input [`W-1:0] d,\n"
        "           output logic [`W-1:0] q);\n"
        "  logic [`W-1:0] a;\n"
        "  always_ff @(posedge clk_a) a <= d;\n"
        "  always_ff @(posedge clk_b) q <= a;\n"
        "endmodule\n"
    )
    sdc = root / "top.sdc"
    sdc.write_text(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    return src, inc, sdc


def _lint(frontend: str, src: Path, sdc: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["lint", "--top", "top", "--frontend", frontend, "--sdc", str(sdc)]
        + list(extra)
        + [str(src)],
    )
    return result.exit_code, result.output


@pytest.mark.parametrize(
    "frontend",
    [
        pytest.param(
            "yosys",
            marks=pytest.mark.skipif(YOSYS is None, reason="yosys not on PATH"),
        ),
        pytest.param(
            "slang",
            marks=pytest.mark.skipif(
                not PYSLANG_INSTALLED, reason="pyslang not installed"
            ),
        ),
    ],
)
def test_incdir_reaches_the_frontend(tmp_path: Path, monkeypatch, frontend) -> None:
    src, inc, sdc = _design(tmp_path)
    if frontend == "yosys":
        # Pin the built-in read_verilog path; the plugin is covered below.
        monkeypatch.delenv("RTL_BUDDY_SLANG_PLUGIN", raising=False)

    code, out = _lint(frontend, src, sdc)
    assert code == 2, out

    code, out = _lint(frontend, src, sdc, "--incdir", str(inc))
    # The a -> q crossing is an unsynchronised bus: the design is bad on
    # purpose so a violation proves the analysis ran on the real width.
    assert code == 1, out
    assert f"incdir:   {inc}" in out
    assert "CDC-" in out


@pytest.mark.skipif(YOSYS is None, reason="yosys not on PATH")
@pytest.mark.skipif(
    not os.environ.get("RTL_BUDDY_SLANG_PLUGIN"),
    reason="RTL_BUDDY_SLANG_PLUGIN not set",
)
def test_incdir_reaches_read_slang(tmp_path: Path) -> None:
    src, inc, sdc = _design(tmp_path)
    code, out = _lint("yosys", src, sdc, "--single-unit")
    assert code == 2, out
    code, out = _lint("yosys", src, sdc, "--single-unit", "-I", str(inc))
    assert code == 1, out


@pytest.mark.skipif(YOSYS is None, reason="yosys not on PATH")
def test_relative_incdir_resolves_against_the_project_root(
    tmp_path: Path, monkeypatch
) -> None:
    src, inc, sdc = _design(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("RTL_BUDDY_SLANG_PLUGIN", raising=False)
    code, out = _lint(
        "yosys", src, sdc, "--project-root", str(tmp_path), "--incdir", "inc"
    )
    assert code == 1, out
    assert f"incdir:   {inc}" in out


def test_missing_incdir_is_rejected_up_front(tmp_path: Path) -> None:
    src, _inc, sdc = _design(tmp_path)
    code, out = _lint("yosys", src, sdc, "--incdir", str(tmp_path / "nope"))
    assert code == 2
    assert f"--incdir is not a directory: {tmp_path / 'nope'}" in out
