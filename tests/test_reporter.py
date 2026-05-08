"""Reporter unit tests — text/json/sarif rendering of an AnalysisResult."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import _analyze_and_report  # noqa: PLC2701
from rtl_buddy_cdc.cli import OutputFormat  # noqa: I001
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.reporter import (
    AnalysisResult,
    render_json,
    render_sarif,
    render_text,
)
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_single_ff_sync"
JSON_PATH = FIX_DIR / "bad_single_ff_sync.json"
SDC_PATH = FIX_DIR / "bad_single_ff_sync.sdc"


@pytest.fixture(scope="module")
def result() -> AnalysisResult:
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
    return AnalysisResult(
        module=module,
        domains=assign_domains(module),
        crossings=crossings,
        async_crossings=async_crossings,
        spec=spec,
        violations=list(violations),
    )


def test_text_includes_violation(result: AnalysisResult) -> None:
    buf = io.StringIO()
    render_text(result, buf)
    text = buf.getvalue()
    assert "module: bad_single_ff_sync" in text
    assert "[CDC-001]" in text
    assert "no second-stage synchronizer" in text


def test_json_is_well_formed(result: AnalysisResult) -> None:
    buf = io.StringIO()
    render_json(result, buf)
    data = json.loads(buf.getvalue())
    assert data["module"] == "bad_single_ff_sync"
    assert data["summary"]["violations"] == 1
    v = data["violations"][0]
    assert v["rule_id"] == "CDC-001"
    assert v["severity"] == "error"
    # Source location should be populated from the dst flop's src attr.
    assert v["location"]["file"].endswith("bad_single_ff_sync.sv")
    assert v["location"]["start_line"] >= 1


def test_sarif_minimum_shape(result: AnalysisResult) -> None:
    buf = io.StringIO()
    render_sarif(result, buf)
    data = json.loads(buf.getvalue())
    assert data["version"] == "2.1.0"
    runs = data["runs"]
    assert len(runs) == 1
    tool = runs[0]["tool"]["driver"]
    assert tool["name"] == "rtl-buddy-cdc"
    # Rule metadata for every distinct rule_id reported.
    assert {r["id"] for r in tool["rules"]} == {"CDC-001"}
    results = runs[0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "CDC-001"
    assert results[0]["level"] == "error"
    loc = results[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"].endswith("bad_single_ff_sync.sv")
    assert "region" in loc and loc["region"]["startLine"] >= 1


def test_cli_format_dispatch_to_file(tmp_path: Path) -> None:
    """`--format json --output <file>` writes valid JSON to the file."""
    out = tmp_path / "report.json"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, None, OutputFormat.json, out)
    assert code == 1  # one CDC-001 violation
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 1
