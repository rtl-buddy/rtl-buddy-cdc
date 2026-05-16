"""Validate ``render_sarif`` output against the SARIF 2.1.0 JSON schema.

The existing tests in ``test_reporter.py`` are *shape* assertions — they
read specific keys and compare values. This file is the *contract*
check: the entire rendered payload must validate against the official
SARIF 2.1.0 schema. A typo'd field name, a wrong type, or a missing
required sub-field that no shape assertion happens to read still
trips this test with a path-scoped error.

Schema vendored at ``tests/schemas/sarif-2.1.0.json``. Source:
https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json
(OASIS, errata01, 2020-03-23). Ingested 2026-05-16. CI must not reach
out to schemastore.org or docs.oasis-open.org at test time.

The schema declares ``$schema: draft-04`` so ``Draft4Validator`` is the
right engine — using ``Draft7Validator`` here would silently accept
constructs draft-04 doesn't permit.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from jsonschema import Draft4Validator

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import OutputFormat, _analyze_and_report  # noqa: PLC2701
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.reporter import AnalysisResult, render_sarif
from rtl_buddy_cdc.rules import run_all as run_all_rules
from rtl_buddy_cdc.waivers import apply as apply_waivers, parse as parse_waivers

FIX_ROOT = Path(__file__).parent / "fixtures"
SCHEMA_PATH = Path(__file__).parent / "schemas" / "sarif-2.1.0.json"

_SARIF_SCHEMA = json.loads(SCHEMA_PATH.read_text())
_VALIDATOR = Draft4Validator(_SARIF_SCHEMA)


def _validate(payload: dict) -> None:
    """Assert the SARIF payload validates against the 2.1.0 schema.

    Aggregates every error into one assertion message so a single test
    run surfaces all the broken paths at once. Each error reads as
    ``<json-pointer>: <message>`` which is enough to find the field in
    ``reporter.py`` without re-running.
    """
    errors = sorted(
        _VALIDATOR.iter_errors(payload), key=lambda e: list(e.absolute_path)
    )
    if not errors:
        return
    lines = []
    for err in errors:
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        lines.append(f"  {path}: {err.message}")
    raise AssertionError("SARIF payload failed schema validation:\n" + "\n".join(lines))


def _build_result(fixture: str) -> AnalysisResult:
    """Build an ``AnalysisResult`` from a fixture's JSON + SDC pair.

    Mirrors the fixture-loading pattern in ``test_reporter.py`` but
    parametrized so we can exercise multiple render shapes (clean,
    with-violations, with-suppressed) in one place.
    """
    fix_dir = FIX_ROOT / fixture
    json_path = fix_dir / f"{fixture}.json"
    sdc_path = fix_dir / f"{fixture}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
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


def _render(result: AnalysisResult) -> dict:
    buf = io.StringIO()
    render_sarif(result, buf)
    return json.loads(buf.getvalue())


# --- coverage ---------------------------------------------------------------
# Four distinct render paths must validate. Each exercises code in
# ``render_sarif`` / ``_violation_to_sarif`` that the others don't.


@pytest.mark.parametrize(
    "fixture",
    [
        # Clean run: zero violations, zero results, just tool metadata.
        # Exercises the "no rules, no results" branch of render_sarif.
        "good_2ff_sync",
        # Bad runs: one or more violations, each with physicalLocation
        # populated from the cell's `attributes["src"]`.
        "bad_single_ff_sync",
        # Different rule, different cell shape — guard against
        # rule-specific drift.
        "bad_bus_crossing",
        "bad_reset_crossing",
    ],
)
def test_sarif_validates(fixture: str) -> None:
    _validate(_render(_build_result(fixture)))


def test_sarif_validates_with_suppressions() -> None:
    """A run with a waiver-suppressed finding emits the SARIF
    ``suppressions`` field. Schema must accept the suppressed entry
    alongside any kept findings."""
    result = _build_result("bad_single_ff_sync")
    waivers = parse_waivers("waive CDC-001 .* hand-reviewed\n")
    kept, suppressed = apply_waivers(result.violations, waivers)
    waived = AnalysisResult(
        module=result.module,
        domains=result.domains,
        crossings=result.crossings,
        async_crossings=result.async_crossings,
        spec=result.spec,
        violations=list(kept),
        suppressed=list(suppressed),
    )
    payload = _render(waived)
    # Sanity: the suppression we set up is actually present, so the
    # schema check covers the suppression-bearing path.
    results = payload["runs"][0]["results"]
    assert any("suppressions" in r for r in results), (
        "test setup didn't produce a suppressed SARIF entry"
    )
    _validate(payload)


def test_sarif_validates_with_baseline_carryover(tmp_path: Path) -> None:
    """Baseline-carried findings emit SARIF entries with a
    ``suppressions`` field tagged ``carried over from baseline``. The
    schema must accept these alongside kept findings."""
    fix_dir = FIX_ROOT / "good_2ff_sync"
    json_path = fix_dir / "good_2ff_sync.json"
    sdc_path = fix_dir / "good_2ff_sync.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    baseline = tmp_path / "baseline.json"
    # First run at strict sync-depth 3 to surface a CDC-002; emit JSON
    # to use as the baseline for the second run.
    _analyze_and_report(
        json_path,
        sdc_path,
        None,
        OutputFormat.json,
        baseline,
        sync_depth=3,
    )
    out = tmp_path / "report.sarif"
    _analyze_and_report(
        json_path,
        sdc_path,
        None,
        OutputFormat.sarif,
        out,
        sync_depth=3,
        baseline_path=baseline,
    )
    payload = json.loads(out.read_text())
    # Sanity: the carryover entry is present and carries the expected
    # SARIF suppression shape.
    results = payload["runs"][0]["results"]
    assert any(
        any(
            s.get("justification") == "carried over from baseline"
            for s in r.get("suppressions", [])
        )
        for r in results
    ), "test setup didn't produce a baseline-carryover SARIF entry"
    _validate(payload)
