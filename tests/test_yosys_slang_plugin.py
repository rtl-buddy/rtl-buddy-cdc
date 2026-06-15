"""End-to-end yosys-slang plugin (`read_slang`) oracle.

Unlike ``test_yosys_plugin_envvar.py`` — which monkeypatches ``elaborate``
to check only the typer-envvar → kwarg wiring — this test drives the real
plugin: it shells out to ``yosys``, loads the freshly-built
``slang.so``, elaborates a design through ``read_slang``, and runs the
rule pack on the resulting netlist.

The design (``fixtures/slang_pkg_unsync_crossing``) pulls its bus type
from a separately-compiled package via ``import cdc_pkg::*`` — a
SystemVerilog-2017 construct Yosys's built-in ``read_verilog -sv``
rejects. So a green result here is proof the ``--yosys-plugin`` /
RTL_BUDDY_SLANG_PLUGIN path produces a netlist the analyzer can read.

Gated three ways (skips unless all hold), so it is a no-op in every job
except the dedicated ``yosys-slang plugin`` CI job that builds the
plugin and exports the env var:

* ``RTL_BUDDY_SLANG_PLUGIN`` points at an existing ``slang.so``;
* ``yosys`` is on PATH.

Select it explicitly with ``pytest -m yosys_slang``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures" / "slang_pkg_unsync_crossing"
TOP = "slang_pkg_unsync_crossing"

_PLUGIN = os.environ.get("RTL_BUDDY_SLANG_PLUGIN")

pytestmark = [
    pytest.mark.yosys_slang,
    pytest.mark.skipif(
        not _PLUGIN or not Path(_PLUGIN).exists(),
        reason="RTL_BUDDY_SLANG_PLUGIN not set to an existing slang.so",
    ),
    pytest.mark.skipif(
        shutil.which("yosys") is None,
        reason="yosys not on PATH",
    ),
]


def _lint_json(tmp_path: Path) -> dict:
    """Run ``lint`` through the yosys-slang plugin, return parsed JSON.

    Writes to ``--output`` rather than stdout so the yosys subprocess
    chatter can never contaminate the parsed report.
    """
    out = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            TOP,
            "--sdc",
            str(FIX / f"{TOP}.sdc"),
            "--yosys-plugin",
            _PLUGIN,
            "--format",
            "json",
            "--output",
            str(out),
            str(FIX / "cdc_pkg.sv"),
            str(FIX / f"{TOP}.sv"),
        ],
    )
    # Exit 1 == "ran successfully, found unsuppressed violations" per the
    # CLI contract; the bus crossing is meant to fire, so 1 is expected.
    assert result.exit_code == 1, result.output
    return json.loads(out.read_text())


def test_read_verilog_rejects_the_package_typed_port() -> None:
    """Guard rail: confirm the design genuinely needs the plugin.

    If a future Yosys gains package-typed-port support in
    ``read_verilog -sv``, this fixture stops proving the plugin is
    exercised — fail loudly here rather than let the plugin test pass
    for the wrong reason.
    """
    import subprocess

    yosys = shutil.which("yosys")
    assert yosys is not None  # guarded by pytestmark skipif
    proc = subprocess.run(
        [
            yosys,
            "-q",
            "-p",
            f"read_verilog -sv {FIX / 'cdc_pkg.sv'} {FIX / f'{TOP}.sv'}; "
            f"hierarchy -top {TOP}",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, (
        "read_verilog -sv unexpectedly accepted the package-typed port; "
        "the fixture no longer proves read_slang is required"
    )


def test_slang_plugin_bus_crossing_fires_cdc_004(tmp_path: Path) -> None:
    report = _lint_json(tmp_path)

    # The plugin actually elaborated something: an 8-bit async crossing.
    assert report["summary"]["crossings"] >= 1

    cdc_004 = [v for v in report["violations"] if v["rule_id"] == "CDC-004"]
    assert len(cdc_004) == 1, report["violations"]
    assert cdc_004[0]["severity"] == "error"
    assert cdc_004[0]["crossing"]["width"] == 8

    # CDC-011 (unconstrained input) must stay silent: the SDC declares
    # d_in's domain, so the only finding is the bus crossing itself.
    assert [v for v in report["violations"] if v["rule_id"] == "CDC-011"] == []
