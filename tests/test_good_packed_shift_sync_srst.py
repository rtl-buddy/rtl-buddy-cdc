"""Positive-counterpart fixture for issue #301: the packed shift-register
synchroniser of #264 with a *synchronous* reset.

``always_ff @(posedge clk) if (rst) q <= '0; else q <= {q[0], d};``
lowers, after ``proc``, to a multi-bit ``$mux`` in front of the flop's
``D`` — the shift vector on one leg, the constant reset value on the
other. ``D`` is then no longer lane-for-lane the flop's own ``Q`` bits,
so ``_packed_shift_register_depth`` stopped matching and CDC-001 fired
a false "chain depth = 1" on every instance.

The fixture carries both reset shapes: ``sync_hi`` is reset active-high
to all-zeros (Yosys puts the shift vector on the mux's ``A`` leg) and
``sync_lo`` active-low to all-ones (shift vector on ``B``). Beyond the
generic no-violations sweep in ``test_good_fixtures.py`` this file pins
the recognised *depth* (2, via the CDC-002 bar) and the cross-frontend
parity that motivated the fix: the slang frontend emits ``$sdff`` for
the same source (CHANGELOG #86) and was already silent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.rules import run_all as run_all_rules

NAME = "good_packed_shift_sync_srst"
FIX_DIR = Path(__file__).parent / "fixtures" / NAME
JSON = FIX_DIR / f"{NAME}.json"
SDC = FIX_DIR / f"{NAME}.sdc"
SV = FIX_DIR / f"{NAME}.sv"

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None


def _analyse(module) -> tuple[list, list]:
    spec = sdc_mod.parse_file(SDC)
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
    return async_crossings, run_all_rules(module, async_crossings, spec)


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    return netlist.load(JSON)


def test_fixture_carries_the_unfolded_reset_mux(context) -> None:
    """The committed JSON must actually hold the ``$mux``-on-D shape —
    if a future regeneration folds it into ``$sdff``/``$sdffe`` the
    fixture would silently stop testing the #301 fix."""
    module = context
    muxes = [c for c in module.cells.values() if c.type == "$mux"]
    assert len(muxes) == 2, [c.type for c in module.cells.values()]
    legs = set()
    for mux in muxes:
        a_const = all(not isinstance(b, int) for b in mux.connections["A"])
        b_const = all(not isinstance(b, int) for b in mux.connections["B"])
        assert a_const != b_const
        legs.add("A" if b_const else "B")
    # One mux per reset polarity, so both leg positions are exercised.
    assert legs == {"A", "B"}


def test_two_async_crossings_and_no_violations(context) -> None:
    async_crossings, violations = _analyse(context)
    assert len(async_crossings) == 2
    assert violations == [], [(v.rule_id, v.message) for v in violations]


def test_cdc_001_silent(context) -> None:
    """Both packed sync-reset registers are valid 2FF synchronisers —
    the false positive #301 was about exactly this shape."""
    _async_crossings, violations = _analyse(context)
    assert [v for v in violations if v.rule_id == "CDC-001"] == []


def test_depth_recognised_as_two_via_cdc_002(context) -> None:
    """At ``required_depth = 3`` both chains must trip CDC-002 reporting
    "found 2 flop(s)" — pinning that the recogniser counts the chain
    behind the reset mux as depth 2 rather than silently passing."""
    module = context
    async_crossings, _violations = _analyse(module)
    spec = sdc_mod.parse_file(SDC)
    raised = run_all_rules(module, async_crossings, spec, required_depth=3)
    cdc_002 = [v for v in raised if v.rule_id == "CDC-002"]
    assert len(cdc_002) == 2
    assert all("found 2 flop(s)" in v.message for v in cdc_002)


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_slang_frontend_agrees() -> None:
    """Cross-frontend parity (#301): slang emits ``$sdff`` whose ``D``
    is the shift vector directly, so it never needed the look-through.
    Both frontends must now reach the same verdict on the same source."""
    module = elaborate([SV], NAME, frontend=Frontend.slang)
    async_crossings, violations = _analyse(module)
    assert len(async_crossings) == 2
    assert violations == [], [(v.rule_id, v.message) for v in violations]
