"""Waiver parser and application tests, plus an end-to-end CLI run."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod, waivers as waivers_mod
from rtl_buddy_cdc.cli import _analyze_and_report, OutputFormat  # noqa: PLC2701
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_single_ff_sync"
JSON_PATH = FIX_DIR / "bad_single_ff_sync.json"
SDC_PATH = FIX_DIR / "bad_single_ff_sync.sdc"


def test_parse_basic() -> None:
    waivers = waivers_mod.parse(
        """
        # comment line
        waive CDC-001 .*procdff\\$9.* hand-reviewed by jsmith
        waive *       .*generated.*   tool-emitted
        """
    )
    assert len(waivers) == 2
    assert waivers[0].rule_pattern == "CDC-001"
    assert waivers[0].reason.startswith("hand-reviewed")
    assert waivers[1].rule_pattern == "*"


def test_parse_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="line 1"):
        waivers_mod.parse("notavalidline foo bar")


def test_parse_rejects_bad_regex() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        waivers_mod.parse("waive CDC-001 *bogus reason")


def test_apply_suppresses_matching() -> None:
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    module = netlist.load(JSON_PATH)
    spec = sdc_mod.parse_file(SDC_PATH)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    violations = run_all_rules(module, async_crossings, spec)
    assert len(violations) == 1  # baseline: one CDC-001

    waivers = waivers_mod.parse("waive CDC-001 procdff\\$9 reviewed by team\n")
    kept, suppressed = waivers_mod.apply(violations, waivers)
    assert kept == []
    assert len(suppressed) == 1
    assert suppressed[0].waiver.reason == "reviewed by team"


def test_apply_non_matching_keeps_violation() -> None:
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    module = netlist.load(JSON_PATH)
    spec = sdc_mod.parse_file(SDC_PATH)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    violations = run_all_rules(module, async_crossings, spec)

    # Wrong rule id, even though regex matches.
    waivers = waivers_mod.parse("waive CDC-005 .* not the right rule\n")
    kept, suppressed = waivers_mod.apply(violations, waivers)
    assert len(kept) == 1
    assert suppressed == []


def test_cli_waiver_drops_exit_code(tmp_path: Path) -> None:
    """End-to-end: a waiver matching the only violation should drive
    the analyzer exit code back to 0."""
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    waiver_file = tmp_path / "cdc.waivers"
    waiver_file.write_text("waive CDC-001 procdff\\$9 reviewed\n")
    out = tmp_path / "report.txt"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, waiver_file, OutputFormat.text, out)
    assert code == 0
    text = out.read_text()
    assert "Suppressed by waivers" in text
    assert "reviewed" in text


def test_cli_no_waiver_still_fails(tmp_path: Path) -> None:
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    out = tmp_path / "report.txt"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, None, OutputFormat.text, out)
    assert code == 1
