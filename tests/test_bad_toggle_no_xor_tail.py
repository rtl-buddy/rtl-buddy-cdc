"""Negative fixture: toggle synchroniser without XOR-tail.

The fast-to-slow failure mode CDC-013 was designed to catch: a
source-side toggle flop encodes events as level changes, the
destination synchronises the toggle through a 2FF chain, but the
chain tail's Q is used directly without an XOR-tail. Two closely
spaced source events cancel before the destination samples — both
are silently lost.

Sibling ``tests/test_good_pulse_synchronizer.py`` pins the positive
counterpart (toggle + 2FF + XOR-tail). Together the pair verifies
that the XOR-tail recognition added in rtl-buddy-cdc#196 doesn't
over-suppress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_toggle_no_xor_tail"
JSON = FIX_DIR / "bad_toggle_no_xor_tail.json"
SDC = FIX_DIR / "bad_toggle_no_xor_tail.sdc"


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


def test_cdc_013_fires_without_xor_tail(context) -> None:
    """CDC-013 must still fire on the genuine failure mode: toggle
    source + 2FF dst chain WITHOUT XOR-tail. The XOR-tail
    suppression added in rtl-buddy-cdc#196 must not over-suppress."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_013 = [v for v in violations if v.rule_id == "CDC-013"]
    assert len(cdc_013) == 1, (
        f"expected exactly one CDC-013, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    v = cdc_013[0]
    assert v.severity == "warning"
    assert "toggle-synchroniser event-loss risk" in v.message
    assert "D = en ? ~Q : Q" in v.message
