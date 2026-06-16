"""P0 visibility (issue #263) — under-resolution is reported, never hidden.

A flop clocked through a transparent latch (``bad_unresolved_clock_latch``)
traces to domain-unknown: the clock-root tracer does not follow a ``$dlatch``
Q, so the flop is silently excluded from CDC analysis. The P0 change surfaces
that exclusion:

  - JSON ``summary.domain_unknown`` (int) counts the unresolved flops, and
    ``domain_unknown_flops`` lists a bounded sample of their cell names.
  - The text report emits a prominent ⚠ warning line.

This is REPORT-ONLY: it must not change any classification. The same fixture
carries a real clk_a→clk_b crossing, so the test also pins parity — the
crossing/violation counts are identical to a from-scratch analysis that has
no knowledge of the new diagnostic.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import OutputFormat, _analyze_and_report  # noqa: PLC2701
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.reporter import (
    JSON_CONTRACT,
    AnalysisResult,
    render_json,
    render_text,
)
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_unresolved_clock_latch"
JSON = FIX_DIR / "bad_unresolved_clock_latch.json"
SDC = FIX_DIR / "bad_unresolved_clock_latch.sdc"


def _skip_if_missing() -> None:
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")


def test_summary_domain_unknown_counts_the_unresolved_flop(tmp_path: Path) -> None:
    """``summary.domain_unknown`` reports the one latch-clocked flop, and
    ``domain_unknown_flops`` lists its cell name."""
    _skip_if_missing()
    out = tmp_path / "report.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    assert payload["summary"]["domain_unknown"] == 1
    # The fixture has three flops; exactly one is unresolved.
    assert payload["summary"]["flops"] == 3
    assert len(payload["domain_unknown_flops"]) == 1
    # The sample list never exceeds the full count.
    assert len(payload["domain_unknown_flops"]) <= payload["summary"]["domain_unknown"]


def test_domain_unknown_is_pinned_in_json_contract() -> None:
    """The new key is part of the downstream contract so ``rtl_buddy`` can
    rely on it to detect coverage degradation."""
    assert JSON_CONTRACT["summary.domain_unknown"] is int


def test_text_report_emits_prominent_unresolved_warning(tmp_path: Path) -> None:
    """The text report carries the ⚠ line naming the count and total."""
    _skip_if_missing()
    out = tmp_path / "report.txt"
    _analyze_and_report(JSON, SDC, None, OutputFormat.text, out, color=False)
    text = out.read_text()
    assert "⚠ 1 of 3 flops have unresolved clock domain" in text
    assert "excluded from CDC analysis" in text


def _from_scratch(json_path: Path, sdc_path: Path) -> tuple[int, int]:
    """Analyse a fixture WITHOUT touching the reporter — the ground-truth
    crossing/violation counts the P0 diagnostic must not perturb."""
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    violations = run_all_rules(module, async_crossings, spec)
    return len(crossings), len(violations)


def test_parity_crossings_and_violations_unchanged(tmp_path: Path) -> None:
    """PARITY: the new diagnostic is additive. The crossing/violation
    counts the full ``analyze`` path reports must match a from-scratch
    analysis that never invokes the reporter — proving no crossing or
    violation was lost (or gained) by the under-resolution being made
    visible."""
    _skip_if_missing()
    plain_crossings, plain_violations = _from_scratch(JSON, SDC)
    out = tmp_path / "report.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    assert payload["summary"]["crossings"] == plain_crossings
    assert payload["summary"]["violations"] == plain_violations
    # And the real crossing/violation is genuinely present (the fixture
    # would be a vacuous parity anchor if it had none).
    assert plain_crossings >= 1
    assert plain_violations >= 1


def test_render_json_domain_unknown_zero_on_fully_resolved() -> None:
    """A result with every flop resolved reports ``domain_unknown == 0``
    and an empty sample list — the diagnostic stays quiet when there is
    nothing to flag."""
    _skip_if_missing()
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    domains = [d for d in assign_domains(module) if d.clock is not None]
    result = AnalysisResult(
        module=module,
        domains=domains,
        crossings=[],
        async_crossings=[],
        spec=spec,
        violations=[],
    )
    buf = io.StringIO()
    render_json(result, buf)
    payload = json.loads(buf.getvalue())
    assert payload["summary"]["domain_unknown"] == 0
    assert payload["domain_unknown_flops"] == []
    # And no warning line in text.
    tbuf = io.StringIO()
    render_text(result, tbuf, color=False)
    assert "unresolved clock domain" not in tbuf.getvalue()
