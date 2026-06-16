"""End-to-end yosys-slang plugin (`read_slang`) oracle for
``--allow-use-before-declare``.

The fixture (``fixtures/slang_use_before_decl``) references a net
before its declaration in the same module — valid RTL that read_verilog
accepts but yosys-slang rejects by default. A green lint proves
rtl-buddy-cdc injects ``--allow-use-before-declare`` so the read_slang
frontend stays as lenient as read_verilog.

Gated like ``test_yosys_slang_plugin.py``: a no-op unless the plugin is
built and ``RTL_BUDDY_SLANG_PLUGIN`` points at it, plus ``yosys`` on
PATH. Select explicitly with ``pytest -m yosys_slang``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures" / "slang_use_before_decl"
TOP = "slang_use_before_decl"

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


def test_read_slang_without_flag_rejects_use_before_decl() -> None:
    """Guard rail: a bare ``read_slang`` (no ``--allow-use-before-declare``)
    must *fail* on this fixture, else the flag rtl-buddy-cdc injects is
    not doing the work and a default change would slip through."""
    yosys = shutil.which("yosys")
    assert yosys is not None  # guarded by pytestmark skipif
    assert _PLUGIN is not None
    proc = subprocess.run(
        [
            yosys,
            "-q",
            "-p",
            (
                f"plugin -i {_PLUGIN}; "
                f"read_slang --std 1800-2017 --top {TOP} "
                f"{FIX / f'{TOP}.sv'}; "
                f"hierarchy -top {TOP}"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, (
        "read_slang unexpectedly accepted the use-before-declaration "
        "without --allow-use-before-declare; the fixture no longer "
        "proves the flag is required"
    )


def test_slang_plugin_elaborates_use_before_decl(tmp_path: Path) -> None:
    """With the flag (injected by rtl-buddy-cdc), the same block
    elaborates into a clean 2FF synchroniser — no violations."""
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
            str(FIX / f"{TOP}.sv"),
        ],
    )
    # Exit 0 == clean (the design is a textbook 2FF sync); the point is
    # that elaboration succeeded rather than failing with exit 2.
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert report["summary"]["crossings"] >= 1
    assert report["summary"]["violations"] == 0
