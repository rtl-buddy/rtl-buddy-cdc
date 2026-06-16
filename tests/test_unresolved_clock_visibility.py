"""P0 visibility (issue #263) — under-resolution is reported, never hidden.

The P0 change surfaces flops whose clock root could not be traced
(``FlopDomain.clock is None``) so an under-resolved run no longer reads
like a clean, complete one:

  - JSON ``summary.domain_unknown`` (int) counts the unresolved flops, and
    ``domain_unknown_flops`` lists a bounded sample of their cell names.
  - The text report emits a prominent ⚠ warning line.

This is REPORT-ONLY: it must not change any classification.

P2 update (issue #263): the clock-root tracer now follows a clock-path
``$dlatch``/``$_DLATCH_*`` Q. ``bad_unresolved_clock_latch`` clocks a flop
through such a latch — at P0 that flop was domain-unknown, and this file
pinned the diagnostic against it. With P2 active that flop RESOLVES to
``clk_a``, so the fixture's expectation moves here: domain_unknown drops to
0 (``test_latch_clocked_flop_now_resolves``). The P0 ``>0`` machinery (the
non-zero count, the ⚠ warning line) is still exercised against
``deep_clock_divider_chain`` — a flop reached only through a >16-hop divider
chain that stays domain-unknown at the default trace depth regardless of the
latch change. The parity anchor (a real clk_a→clk_b crossing the diagnostic
must not perturb) lives on both fixtures.
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

# A flop reached only through a >16-hop divider chain stays domain-unknown at
# the default trace depth no matter what P2 teaches the tracer about latches —
# the durable anchor for the P0 ``>0`` machinery (count, ⚠ warning line).
DEEP_DIR = Path(__file__).parent / "fixtures" / "deep_clock_divider_chain"
DEEP_JSON = DEEP_DIR / "deep_clock_divider_chain.json"
DEEP_SDC = DEEP_DIR / "deep_clock_divider_chain.sdc"


def _skip_if_missing() -> None:
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")


def _skip_if_deep_missing() -> None:
    if not DEEP_JSON.exists():
        pytest.skip(f"fixture not built: {DEEP_JSON}")


def test_latch_clocked_flop_now_resolves(tmp_path: Path) -> None:
    """P2: the clock-path-latch-clocked flop now resolves, so this fixture
    reports ``domain_unknown == 0``. At P0 it was 1; P2 teaches ``_trace`` to
    follow the ``$dlatch`` Q back to ``clk_a``, moving the under-resolution
    expectation off this fixture entirely."""
    _skip_if_missing()
    out = tmp_path / "report.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    assert payload["summary"]["domain_unknown"] == 0
    # All three flops resolve now (was 2 of 3 at P0).
    assert payload["summary"]["flops"] == 3
    assert payload["domain_unknown_flops"] == []


def test_summary_domain_unknown_counts_the_unresolved_flops(tmp_path: Path) -> None:
    """``summary.domain_unknown`` reports the deep-chain unresolved flops, and
    ``domain_unknown_flops`` lists a bounded sample of their cell names."""
    _skip_if_deep_missing()
    out = tmp_path / "report.json"
    _analyze_and_report(DEEP_JSON, DEEP_SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    # The 30-stage divider chain leaves a stable set of flops unresolved at the
    # default depth (the deep tap plus the stages past hop 16).
    assert payload["summary"]["domain_unknown"] > 0
    assert payload["domain_unknown_flops"]
    # The sample list never exceeds the full count (and is capped at 20).
    assert len(payload["domain_unknown_flops"]) <= payload["summary"]["domain_unknown"]
    assert len(payload["domain_unknown_flops"]) <= 20


def test_domain_unknown_is_pinned_in_json_contract() -> None:
    """The new key is part of the downstream contract so ``rtl_buddy`` can
    rely on it to detect coverage degradation."""
    assert JSON_CONTRACT["summary.domain_unknown"] is int


def test_text_report_emits_prominent_unresolved_warning(tmp_path: Path) -> None:
    """The text report carries the ⚠ line naming the count and total."""
    _skip_if_deep_missing()
    out = tmp_path / "report.txt"
    _analyze_and_report(DEEP_JSON, DEEP_SDC, None, OutputFormat.text, out, color=False)
    text = out.read_text()
    assert "flops have unresolved clock domain" in text
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
