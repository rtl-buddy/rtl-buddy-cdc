"""Positive-counterpart fixture for issue #304: the separate-flop 2FF
synchroniser of good_2ff_sync with a per-stage *synchronous* reset —
the follow-up rtl-buddy-cdc#303 left open after fixing the packed form.

``if (rst) begin s1 <= '0; s2 <= '0; end else begin s1 <= d; s2 <= s1; end``
lowers, after ``proc``, to a 1-bit ``$mux`` in front of each stage's
``D``. ``_sync_chain_depth`` then saw ``s1.Q`` read by a non-flop cell
and stopped at depth 1, and ``_chain_has_inter_stage_comb`` read the
same reset mux as a gate between the stages, so CDC-014 fired on every
instance — while the slang frontend (``$sdff`` per stage) was silent.

The fixture carries both reset shapes: ``sync_hi`` is reset active-high
to zero (data on the mux's ``A`` leg) and ``sync_lo`` active-low to one
(data on ``B``). Beyond the generic no-violations sweep in
``test_good_fixtures.py`` this file pins the recognised *depth* (2, via
the CDC-002 bar), that CDC-014 specifically stays silent, and the
cross-frontend parity that motivated the fix.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.rules import run_all as run_all_rules

NAME = "good_2ff_sync_srst"
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


def test_fixture_carries_the_unfolded_reset_muxes(context) -> None:
    """The committed JSON must actually hold the ``$mux``-on-D shape on
    *every* stage — if a future regeneration folds them into
    ``$sdff`` the fixture would silently stop testing the #304 fix."""
    module = context
    muxes = [c for c in module.cells.values() if c.type == "$mux"]
    # Two chains × two stages, one 1-bit mux each.
    assert len(muxes) == 4, [c.type for c in module.cells.values()]
    assert all(len(m.connections["Y"]) == 1 for m in muxes)
    legs = set()
    for mux in muxes:
        a_const = all(not isinstance(b, int) for b in mux.connections["A"])
        b_const = all(not isinstance(b, int) for b in mux.connections["B"])
        assert a_const != b_const
        legs.add("A" if b_const else "B")
    # One polarity per chain, so both leg positions are exercised.
    assert legs == {"A", "B"}


def test_two_async_crossings_and_no_violations(context) -> None:
    async_crossings, violations = _analyse(context)
    assert len(async_crossings) == 2
    assert violations == [], [(v.rule_id, v.message) for v in violations]


def test_cdc_014_and_cdc_001_silent(context) -> None:
    """The false positive #304 was CDC-014 reading the stage-2 reset mux
    as a gate between the stages; CDC-001 must not take over either."""
    _async_crossings, violations = _analyse(context)
    assert [v.rule_id for v in violations if v.rule_id in ("CDC-014", "CDC-001")] == []


def test_depth_recognised_as_two_via_cdc_002(context) -> None:
    """At ``required_depth = 3`` both chains must trip CDC-002 reporting
    "found 2 flop(s)" — pinning that the walker steps over the reset
    mux to the second stage rather than silently passing."""
    module = context
    async_crossings, _violations = _analyse(module)
    spec = sdc_mod.parse_file(SDC)
    raised = run_all_rules(module, async_crossings, spec, required_depth=3)
    cdc_002 = [v for v in raised if v.rule_id == "CDC-002"]
    assert len(cdc_002) == 2
    assert all("found 2 flop(s)" in v.message for v in cdc_002)


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_slang_frontend_agrees() -> None:
    """Cross-frontend parity (#304): slang emits an ``$sdff`` per stage
    whose ``D`` is the data directly, so it never needed the step-over.
    Both frontends must now reach the same verdict on the same source."""
    module = elaborate([SV], NAME, frontend=Frontend.slang)
    async_crossings, violations = _analyse(module)
    assert len(async_crossings) == 2
    assert violations == [], [(v.rule_id, v.message) for v in violations]
