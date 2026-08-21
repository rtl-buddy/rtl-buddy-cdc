"""In-RTL pragmas applied end-to-end through ``lint`` (issue #42).

Phase 2 wires :func:`rtl_buddy_cdc.pragma.scan` into the waiver path of
the source-driven ``lint`` command. The fixture is
``pragma_waived_single_ff``: a CDC-001 crossing waived in place by an
``// rbcdc: disable-rule CDC-001`` comment above the offending flop.

The frontend is irrelevant to the pragma (sources are scanned as
text), so the slang path — the one CI's coverage job actually installs
— carries the assertions, and a yosys-gated twin proves the
independence when a yosys binary is around.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import pragma
from rtl_buddy_cdc.cli import app

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None

FIX = Path(__file__).parent / "fixtures" / "pragma_waived_single_ff"
SV = FIX / "pragma_waived_single_ff.sv"
SDC = FIX / "pragma_waived_single_ff.sdc"
NETLIST = FIX / "pragma_waived_single_ff.json"

runner = CliRunner()


def _lint(*extra: str, frontend: str = "slang") -> object:
    return runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            frontend,
            "--top",
            "pragma_waived_single_ff",
            "--sdc",
            str(SDC),
            *extra,
            str(SV),
        ],
    )


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_pragma_suppresses_and_exits_zero() -> None:
    """The whole point: a pragma next to the flop turns a failing run
    into a passing one, with the finding still on the report."""
    result = _lint()
    assert result.exit_code == 0, result.output
    assert "No rule violations." in result.output
    assert "Suppressed by waivers (1)" in result.output
    assert "CDC-001" in result.output
    assert "hand-reviewed: q_out is quasi-static" in result.output


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_text_report_shows_the_pragma_source_location() -> None:
    """A pragma renders as ``pragma <file>:<line>``, not as a
    waiver-file line number — the two are distinguishable at a glance."""
    result = _lint()
    assert "(pragma " in result.output
    assert f"{SV}:26" in result.output
    assert "waiver line" not in result.output


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_json_suppressed_carries_file_and_line() -> None:
    result = _lint("--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["suppressed"] == 1
    (entry,) = payload["suppressed"]
    assert entry["rule_id"] == "CDC-001"
    waiver = entry["waiver"]
    assert waiver["origin"] == str(SV)
    assert waiver["source_line"] == 26
    assert waiver["reason"] == "hand-reviewed: q_out is quasi-static"


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_waiver_file_entries_still_render_as_waiver_lines(tmp_path: Path) -> None:
    """The pragma and the ``--waivers`` file coexist: an unrelated
    file waiver keeps the old rendering."""
    wf = tmp_path / "cdc.waivers"
    wf.write_text("waive CDC-999 nothing-matches placeholder\n")
    result = _lint("--waivers", str(wf))
    assert result.exit_code == 0, result.output
    assert "(pragma " in result.output


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not on PATH")
def test_pragma_is_frontend_independent() -> None:
    """Same suppression through the yosys frontend — the scan reads
    the sources as text, not through whatever elaborated them."""
    result = _lint(frontend="yosys")
    assert result.exit_code == 0, result.output
    assert "Suppressed by waivers (1)" in result.output


def test_analyze_does_not_honour_pragmas() -> None:
    """``analyze`` starts from an elaborated netlist and has no
    sources to scan — the finding stands and the run fails."""
    result = runner.invoke(
        app,
        ["analyze", "--netlist", str(NETLIST), "--sdc", str(SDC)],
    )
    assert result.exit_code == 1, result.output
    assert "CDC-001" in result.output
    assert "Suppressed by waivers" not in result.output


def test_analyze_help_documents_the_gap() -> None:
    result = runner.invoke(app, ["analyze", "--help"])
    assert result.exit_code == 0
    assert "rbcdc" in result.output


def test_violation_source_ref_returns_none_without_a_location() -> None:
    """The location resolver is total: a violation the analyzer can't
    place (no cell, or a cell carrying no ``src``) resolves to ``None``
    and is therefore never waived by a pragma."""
    from rtl_buddy_cdc import netlist as netlist_mod
    from rtl_buddy_cdc.cli import _violation_source_ref  # noqa: PLC2701
    from rtl_buddy_cdc.rules import Violation

    module = netlist_mod.load(NETLIST)
    unplaced = Violation(rule_id="CDC-BBX", severity="error", message="opaque")
    assert _violation_source_ref(module, unplaced) is None
    missing_cell = dataclasses.replace(unplaced, cell_name="$no_such_cell")
    assert _violation_source_ref(module, missing_cell) is None


# --- block scoping, end to end (issue #43) ----------------------------------

BLOCK_FIX = Path(__file__).parent / "fixtures" / "pragma_block_scope"
BLOCK_SV = BLOCK_FIX / "pragma_block_scope.sv"
BLOCK_SDC = BLOCK_FIX / "pragma_block_scope.sdc"


def _lint_block(*extra: str, frontend: str = "slang") -> object:
    return runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            frontend,
            "--top",
            "pragma_block_scope",
            "--sdc",
            str(BLOCK_SDC),
            *extra,
            str(BLOCK_SV),
        ],
    )


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_block_scope_suppresses_inside_and_keeps_outside() -> None:
    """The fixture has two identical CDC-001 crossings; only the one
    inside the disable/enable block is waived, so the run still fails."""
    result = _lint_block("--format", "json")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["violations"] == 1
    assert payload["summary"]["suppressed"] == 1

    (kept,) = payload["violations"]
    (sup,) = payload["suppressed"]
    kept_line = kept["location"]["start_line"]
    sup_line = sup["location"]["start_line"]
    waiver = sup["waiver"]
    # The suppressed one is inside [start, end); the kept one is after.
    assert waiver["origin"] == str(BLOCK_SV)
    assert waiver["source_line"] <= sup_line < waiver["end_line"]
    assert kept_line >= waiver["end_line"]


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_block_scope_renders_its_range_in_the_text_report() -> None:
    """Optional polish from #43: a closed block shows as a range, so a
    block-scoped pragma is visibly narrower than a file-scoped one."""
    result = _lint_block()
    assert f"(pragma {BLOCK_SV}:37-42)" in result.output


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not on PATH")
def test_block_scope_is_frontend_independent() -> None:
    result = _lint_block(frontend="yosys")
    assert result.exit_code == 1, result.output
    assert "Suppressed by waivers (1)" in result.output
    assert f"(pragma {BLOCK_SV}:37-42)" in result.output


def test_unclosed_pragma_still_renders_without_a_range() -> None:
    """The phase-2 form (no `enable-rule`) keeps its single-location
    rendering — the two shapes stay distinguishable."""
    from rtl_buddy_cdc.reporter import _waiver_provenance  # noqa: PLC2701

    (open_block,) = pragma.scan([SV])
    assert open_block.end_line is None
    assert _waiver_provenance(open_block) == f"pragma {SV}:26"
