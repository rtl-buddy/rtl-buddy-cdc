"""Positive-counterpart fixture for issue #264: a 2-FF synchronizer
coded as a single packed shift register (`sync_sr <= {sync_sr[0],
src_q}`) is logically identical to the separate-flop form and must
stay silent on CDC-001.

The `good_packed_shift_sync` fixture is also covered by the generic
no-violations sweep in `test_good_fixtures.py`; this file adds the
sharper assertion that the analyzer recognises the *depth* as 2 (not
just "no violations"), so a future regression that under-counts the
packed chain is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "good_packed_shift_sync"
JSON = FIX_DIR / "good_packed_shift_sync.json"
SDC = FIX_DIR / "good_packed_shift_sync.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings


def test_one_async_crossing(context) -> None:
    _module, async_crossings = context
    assert len(async_crossings) == 1


def test_cdc_001_silent(context) -> None:
    """The packed shift register is a valid 2FF synchronizer — the
    false positive #264 was about exactly this shape."""
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    assert [v for v in violations if v.rule_id == "CDC-001"] == []


def test_depth_recognised_as_two_via_cdc_002(context) -> None:
    """At required_depth = 3 the packed depth-2 chain must trip CDC-002,
    reporting "found 2 flop(s)". This pins that the recognizer counts
    the chain as depth 2 rather than silently passing or mis-counting."""
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings, required_depth=3)
    cdc_002 = [v for v in violations if v.rule_id == "CDC-002"]
    assert len(cdc_002) == 1
    assert "found 2 flop(s)" in cdc_002[0].message


def test_no_cdc_002_at_default_depth(context) -> None:
    """Depth 2 satisfies the default required_depth = 2, so CDC-002 stays
    silent — the packed form is accepted on its own merits."""
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    assert [v for v in violations if v.rule_id == "CDC-002"] == []
