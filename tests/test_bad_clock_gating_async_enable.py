"""Raw-AND clock gate with a foreign-domain enable: CDC-010.

The enable lives in `clk_b`'s domain; the AND output drives a flop's
CLK on `clk_a`'s branch. The clocks are declared async. Every enable
transition can chop the gated clock into runt pulses — the CDC-010
failure mode, on a raw `$and` gate instead of a named control pin.

Issue #177 pinned this as invisible to the rule pack. Since
rtl-buddy-cdc#408 CDC-010 reads the gate's control leg off the
clock-root walk (the leg that is not the gate's clock), and the walk
no longer follows the enable flop's `Q` back to `clk_b` as a divided
clock — which used to decline the gate as a two-clock combine
(CDC-023) and leave the gated flop domain-unknown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_clock_gating_async_enable"
JSON = FIX_DIR / "bad_clock_gating_async_enable.json"
SDC = FIX_DIR / "bad_clock_gating_async_enable.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_raw_and_clock_gate_with_async_enable_is_cdc010(context) -> None:
    """The enable leg is a control, not a second clock: CDC-010 fires
    on the gate's enable pin, CDC-023 does not, and the gated flop
    resolves to `clk_a`."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert sorted(v.rule_id for v in violations) == ["CDC-010"]
    (v,) = violations
    assert v.severity == "error"
    assert "control pin B" in v.message
    assert "in domain clk_b" in v.message
    assert "['clk_a']" in v.message
