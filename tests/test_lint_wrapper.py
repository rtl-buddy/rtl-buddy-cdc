"""End-to-end tests for the standalone ``rtl-buddy-cdc lint`` wrapper.

The wrapper shells out to yosys to elaborate and flatten the design,
then calls the analyzer. Tests are skipped when no yosys binary is on
PATH so they can run unattended on hosts that haven't built one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

YOSYS = shutil.which("yosys")
if YOSYS is None:
    pytest.skip(
        "yosys not on PATH — skipping lint-wrapper end-to-end tests",
        allow_module_level=True,
    )

FIX_ROOT = Path(__file__).parent / "fixtures"
runner = CliRunner()


def test_lint_golden_clean() -> None:
    """ip_cdc_handshake should elaborate, analyze, and pass without
    any rule violations (exit code 0)."""
    fix = FIX_ROOT / "ip_cdc_handshake"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "ip_cdc_handshake",
            "--sdc",
            str(fix / "ip_cdc_handshake.sdc"),
            str(fix / "ip_cdc_sync.sv"),
            str(fix / "ip_cdc_handshake.sv"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No rule violations." in result.output
    # The analyzer banner should mention the elaborated module.
    assert "ip_cdc_handshake" in result.output
    assert "PASS" in result.output


def test_lint_bad_case_returns_nonzero() -> None:
    """bad_single_ff_sync trips CDC-001 and the wrapper must propagate
    a non-zero exit code so CI can gate on it."""
    fix = FIX_ROOT / "bad_single_ff_sync"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "bad_single_ff_sync",
            "--sdc",
            str(fix / "bad_single_ff_sync.sdc"),
            str(fix / "bad_single_ff_sync.sv"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "CDC-001" in result.output
    assert "unsynchronized control crossing" in result.output
    assert "FAIL" in result.output


def test_lint_keep_json(tmp_path: Path) -> None:
    """--keep-json should drop the elaborated netlist at the requested
    path, even on the success path."""
    fix = FIX_ROOT / "ip_cdc_handshake"
    out = tmp_path / "kept.json"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "ip_cdc_handshake",
            "--sdc",
            str(fix / "ip_cdc_handshake.sdc"),
            "--keep-json",
            str(out),
            str(fix / "ip_cdc_sync.sv"),
            str(fix / "ip_cdc_handshake.sv"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    # write_json output is a JSON object beginning with `{`.
    assert out.read_text().lstrip().startswith("{")


def test_lint_greybox_analyses_the_module_body(tmp_path: Path) -> None:
    """``--greybox`` keeps the named module as a boundary cell *with* its
    internals (rtl-buddy-cdc#261), so the subtree is analysed once on its
    own body and its finding is lifted into the parent's report naming
    every instance it covers. Unlike ``--blackbox`` it needs no
    yosys-slang plugin — it is a plain ``setattr -mod -set blackbox 1``
    before ``flatten``."""
    fix = FIX_ROOT / "bbx_shared_internal_violation"
    keep = tmp_path / "grey.json"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "top",
            "--sdc",
            str(fix / "bbx_shared_internal_violation.sdc"),
            "--greybox",
            "xsync",
            "--sync-depth",
            "3",
            "--keep-json",
            str(keep),
            str(fix / "bbx_shared_internal_violation.sv"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "[inside `xsync` — analysed once, 2 instances: u_a, u_b]" in result.output
    assert "CDC-002" in result.output
    # No coverage gap is reported: the block was analysed, not declined.
    assert "CDC-BBX" not in result.output

    # The intermediate netlist really is a greybox: blackbox-attributed
    # (so ``flatten`` left it standing) AND still carrying its cells.
    import json

    mods = json.loads(keep.read_text())["modules"]
    assert mods["xsync"]["attributes"]["blackbox"].endswith("1")
    assert mods["xsync"]["cells"]


def test_lint_rejects_the_same_module_as_both_blackbox_and_greybox() -> None:
    """Contradictory: the ``read_slang`` stub would win and there would be
    no body left to analyse. Fail loudly rather than silently picking."""
    fix = FIX_ROOT / "bbx_shared_internal_violation"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "top",
            "--sdc",
            str(fix / "bbx_shared_internal_violation.sdc"),
            "--greybox",
            "xsync",
            "--blackbox",
            "xsync",
            str(fix / "bbx_shared_internal_violation.sv"),
        ],
    )
    assert result.exit_code == 2
    assert "name the same module(s): xsync" in result.output
