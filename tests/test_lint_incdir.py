"""``lint --incdir`` (``-I``): `` `include`` search directories (#311).

A source that includes a header by bare name from another directory only
elaborates when that directory is passed. The script-capture tests check
what each yosys read command receives without running yosys; the
end-to-end tests run each frontend that is installed on the repro from
the issue.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy_cdc.cli as cli
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontends import yosys as yosys_fe
from rtl_buddy_cdc.frontends.yosys import YosysError

runner = CliRunner()

_PLUGIN = os.environ.get("RTL_BUDDY_SLANG_PLUGIN")
_HAVE_PLUGIN = bool(_PLUGIN) and Path(str(_PLUGIN)).exists()
_HAVE_YOSYS = shutil.which("yosys") is not None
_HAVE_PYSLANG = importlib.util.find_spec("pyslang") is not None


def _design(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write the issue's repro: ``src/top.sv`` includes ``consts.vi`` from ``inc/``."""
    inc = tmp_path / "inc"
    src = tmp_path / "src"
    inc.mkdir()
    src.mkdir()
    (inc / "consts.vi").write_text("`define W 4\n")
    top = src / "top.sv"
    top.write_text(
        '`include "consts.vi"\n'
        "module top (input logic clk, input logic [`W-1:0] d,"
        " output logic [`W-1:0] q);\n"
        "  always_ff @(posedge clk) q <= d;\n"
        "endmodule\n"
    )
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n")
    return top, inc, sdc


def _lint(top: Path, sdc: Path, out: Path, extra: list[str]):
    return runner.invoke(
        app,
        [
            "lint",
            "--top",
            "top",
            "--sdc",
            str(sdc),
            "--format",
            "json",
            "--output",
            str(out),
            *extra,
            str(top),
        ],
    )


# --- CLI wiring ------------------------------------------------------------


class _Captured(Exception):
    pass


def test_incdirs_reach_the_frontend_absolute_and_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both spellings repeat, keep their order, and resolve against the cwd."""
    top, inc, sdc = _design(tmp_path)
    (tmp_path / "inc2").mkdir()
    box: dict[str, object] = {}

    def fake(*_args: object, **kwargs: object):
        box.update(kwargs)
        raise _Captured

    monkeypatch.setattr(cli, "elaborate_with_blackboxes", fake)
    monkeypatch.chdir(tmp_path)
    result = _lint(top, sdc, tmp_path / "o.json", ["--incdir", "inc2", "-I", "inc"])
    assert isinstance(result.exception, _Captured), result.output
    assert box["incdirs"] == [(tmp_path / "inc2").resolve(), inc.resolve()]


def test_no_incdir_passes_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top, _inc, sdc = _design(tmp_path)
    box: dict[str, object] = {}

    def fake(*_args: object, **kwargs: object):
        box.update(kwargs)
        raise _Captured

    monkeypatch.setattr(cli, "elaborate_with_blackboxes", fake)
    result = _lint(top, sdc, tmp_path / "o.json", [])
    assert isinstance(result.exception, _Captured), result.output
    assert box["incdirs"] == []


@pytest.mark.parametrize("bad", ["missing", "file"])
def test_incdir_must_be_an_existing_directory(tmp_path: Path, bad: str) -> None:
    top, _inc, sdc = _design(tmp_path)
    target = tmp_path / bad
    if bad == "file":
        target.write_text("")
    result = _lint(top, sdc, tmp_path / "o.json", ["--incdir", str(target)])
    assert result.exit_code == 2
    assert "--incdir" in result.output


# --- yosys script ------------------------------------------------------------


def _capture_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> str:
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")
    captured: dict[str, str] = {}

    class _Proc:
        returncode = 1  # stop after the script is captured
        stderr = "stop"
        stdout = ""

    def _fake_run(cmd, capture_output, text):  # noqa: ANN001
        captured["script"] = cmd[3]
        return _Proc()

    monkeypatch.setattr(yosys_fe.subprocess, "run", _fake_run)
    with pytest.raises(YosysError, match="stop"):
        yosys_fe.elaborate(
            [Path("a.sv")], "topmod", yosys_bin=str(fake_yosys), **kwargs
        )
    return captured["script"]


def test_read_verilog_gets_each_incdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _capture_script(
        tmp_path, monkeypatch, incdirs=[Path("/p/inc"), Path("/p/inc/sub")]
    )
    assert "read_verilog -sv -I /p/inc -I /p/inc/sub a.sv;" in script


def test_read_slang_gets_each_incdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "slang.so"
    plugin.write_text("")
    script = _capture_script(
        tmp_path,
        monkeypatch,
        plugin_path=str(plugin),
        single_unit=True,
        incdirs=[Path("/p/inc"), Path("/p/inc/sub")],
    )
    assert "read_slang " in script
    assert "-I /p/inc -I /p/inc/sub " in script
    assert script.index("-I /p/inc ") < script.index(" a.sv")


def test_no_incdirs_leaves_the_read_commands_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _capture_script(tmp_path, monkeypatch)
    assert "read_verilog -sv a.sv;" in script
    assert "-I" not in script


def test_read_slang_rejects_an_incdir_with_a_comma(tmp_path: Path) -> None:
    """read_slang splits ``-I a,b`` into two directories."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")
    plugin = tmp_path / "slang.so"
    plugin.write_text("")
    with pytest.raises(YosysError, match="splits -I values on commas"):
        yosys_fe.elaborate(
            [Path("a.sv")],
            "topmod",
            yosys_bin=str(fake_yosys),
            plugin_path=str(plugin),
            incdirs=[Path("/p/a,b")],
        )


# --- end to end ----------------------------------------------------------------

_FRONTENDS = [
    pytest.param(
        ["--frontend", "yosys"],
        marks=pytest.mark.skipif(not _HAVE_YOSYS, reason="yosys not on PATH"),
        id="read_verilog",
    ),
    pytest.param(
        ["--frontend", "yosys", "--yosys-plugin", str(_PLUGIN), "--single-unit"],
        marks=[
            pytest.mark.yosys_slang,
            pytest.mark.skipif(
                not (_HAVE_YOSYS and _HAVE_PLUGIN),
                reason="yosys or RTL_BUDDY_SLANG_PLUGIN missing",
            ),
        ],
        id="read_slang",
    ),
    pytest.param(
        ["--frontend", "slang"],
        marks=pytest.mark.skipif(not _HAVE_PYSLANG, reason="pyslang not installed"),
        id="pyslang",
    ),
]


@pytest.mark.parametrize("frontend_args", _FRONTENDS)
def test_header_in_an_incdir_elaborates(
    tmp_path: Path, frontend_args: list[str]
) -> None:
    """With ``--incdir`` the design elaborates and lints: one CDC-004 on the
    unconstrained ``d`` bus, the same result as with the header beside the source."""
    top, inc, sdc = _design(tmp_path)
    out = tmp_path / "o.json"
    result = _lint(top, sdc, out, [*frontend_args, "--incdir", str(inc)])
    assert result.exit_code == 1, result.output
    report = json.loads(out.read_text())
    rules = sorted(v["rule_id"] for v in report["violations"])
    assert "CDC-004" in rules


@pytest.mark.parametrize("frontend_args", _FRONTENDS[:2])
def test_header_without_incdir_fails_to_elaborate(
    tmp_path: Path, frontend_args: list[str]
) -> None:
    """The control: without the option the yosys frontends cannot find the header."""
    top, _inc, sdc = _design(tmp_path)
    result = _lint(top, sdc, tmp_path / "o.json", frontend_args)
    assert result.exit_code == 2, result.output
