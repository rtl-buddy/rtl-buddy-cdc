"""Positive fixture: canonical pulse synchroniser.

Source-side toggle flop + 2FF dst chain + XOR-tail on the chain
tail's Q. This is the textbook correct fast-to-slow event-passing
idiom (Cummings SNUG 2008 §6).

CDC-013 must stay silent: the source-side toggle pattern matches
the classifier, but the XOR-tail recognition added in
rtl-buddy-cdc#196 suppresses the finding because the dst-side
reconstructs the pulse correctly.

Sibling ``tests/test_bad_toggle_no_xor_tail.py`` pins the failure
case CDC-013 should still catch (toggle source + 2FF without
XOR-tail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "good_pulse_synchronizer"
JSON = FIX_DIR / "good_pulse_synchronizer.json"
SDC = FIX_DIR / "good_pulse_synchronizer.sdc"


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


def test_cdc_013_suppressed_by_xor_tail(context) -> None:
    """CDC-013 must stay silent: the dst-side XOR-tail is the
    canonical pulse reconstruction; the rule's positive-recognition
    phase (``_has_xor_tail_pulse_recovery``) confirms it and
    suppresses the finding that would otherwise fire on the
    source-side toggle pattern."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_013 = [v for v in violations if v.rule_id == "CDC-013"]
    assert cdc_013 == [], (
        f"CDC-013 false-fired on canonical pulse synchroniser: "
        f"{[v.message for v in cdc_013]}"
    )


def test_no_unexpected_rules_fire(context) -> None:
    """A complete pulse-synchroniser idiom should fire no rule:
    CDC-001/-002 see a valid 2FF chain, CDC-003 sees no comb
    before the first stage, CDC-009 sees no edge-detector pattern,
    CDC-013 (this rule) is now XOR-tail-suppressed. If anything
    fires here it's a regression."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    assert rule_ids == [], (
        f"unexpected rules fired on canonical pulse synchroniser: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
