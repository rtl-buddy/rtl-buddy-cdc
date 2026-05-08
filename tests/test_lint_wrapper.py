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
    assert "no rule violations." in result.output
    # The analyzer banner should mention the elaborated module.
    assert "module: ip_cdc_handshake" in result.output


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
    assert "[CDC-001]" in result.output
    assert "unsynchronized control crossing" in result.output


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
