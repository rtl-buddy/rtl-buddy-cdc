"""Negative-case fixture for CDC-013 — fast-to-slow control-event loss.

Issue #151 (signoff CDC coverage gaps).

A fast-domain toggle is sampled by a 2FF synchroniser in a slower
destination domain. If two source events occur between destination
samples, the toggle returns to its prior value and the destination
observes zero edges. The synchroniser is structurally safe for
metastability; the failure is event accounting / protocol loss,
not metastability.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_fast_to_slow_control_loss"
JSON = FIX_DIR / "bad_fast_to_slow_control_loss.json"
SDC = FIX_DIR / "bad_fast_to_slow_control_loss.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def test_cdc_013_fires_as_warning(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_013 = [v for v in violations if v.rule_id == "CDC-013"]
    assert len(cdc_013) == 1, (
        f"expected exactly one CDC-013, got {[v.rule_id for v in violations]}"
    )
    v = cdc_013[0]
    assert v.severity == "warning"
    assert "toggle-synchroniser event-loss risk" in v.message
    assert "src_clk" in v.message and "dst_clk" in v.message
    assert "D = en ? ~Q : Q" in v.message
    assert "req/ack handshake" in v.message  # fix advice
    assert v.crossing is not None


def test_no_other_rules_fire(context) -> None:
    """The 2FF dst-side synchroniser keeps CDC-001/-002/-003 silent;
    the src flop's D is a $mux not an $and edge-detector so CDC-009
    is silent; the design is single-bit so CDC-004 is silent. Only
    CDC-013 fires."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"CDC-013"}, f"unexpected rules fired: {rule_ids}"
