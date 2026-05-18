"""Negative-case fixture for CDC-009: 1-cycle src pulse → slower dst clock.

Issue #47 (design), implemented in #101. The src flop's D pin is an
edge-detector ``event_q & ~event_d`` whose Q is a single src cycle
wide; the dst clock is 10× slower, so the pulse may land entirely
between dst rising edges. CDC-009 must fire as a single warning on
this crossing; no other rule should fire (the dst-side 2FF sync chain
keeps CDC-001/002 silent, and width=1 keeps CDC-004 silent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_pulse_width_fast_to_slow"
JSON = FIX_DIR / "bad_pulse_width_fast_to_slow.json"
SDC = FIX_DIR / "bad_pulse_width_fast_to_slow.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_single_async_crossing(context) -> None:
    """One 1-bit src→dst crossing (the event_strobe → captured_meta path)."""
    _module, crossings, _spec = context
    assert len(crossings) == 1
    c = crossings[0]
    assert c.width == 1
    assert c.src_flop is not None
    assert c.src_clock == "src_clk"
    assert c.dst_clock == "dst_clk"


def test_cdc_009_fires_once_at_warning(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_009 = [v for v in violations if v.rule_id == "CDC-009"]
    assert len(cdc_009) == 1
    v = cdc_009[0]
    assert v.severity == "warning"
    assert "pulse-width risk" in v.message
    assert "'src_clk'" in v.message
    assert "'dst_clk'" in v.message
    # Render unit on both periods so the user can see the ratio.
    assert "period 2.0" in v.message
    assert "period 20.0" in v.message
    # The fix advice nudges toward the three idioms documented in #47 §5.
    assert "pulse-stretcher" in v.message
    assert "toggle synchronizer" in v.message
    assert "handshake" in v.message


def test_no_other_rules_fire(context) -> None:
    """Only CDC-009 should fire: the dst 2FF chain satisfies CDC-001/002,
    width=1 dodges CDC-004, no comb-before-sync, no glitchy source."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"CDC-009"}
