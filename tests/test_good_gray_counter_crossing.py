"""Positive fixture for a gated gray-coded bus crossing.

A 4-bit gray counter increments in src_clk; its value is sampled by a
dst_clk register only under a synchronized control bit. The destination
does not sample the bus freely, so CDC-004 must not fire."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "good_gray_counter_crossing"
JSON = FIX_DIR / "good_gray_counter_crossing.json"
SDC = FIX_DIR / "good_gray_counter_crossing.sdc"


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
    return module, async_crossings, spec


def test_gated_gray_bus_crossings(context) -> None:
    """The fixture has one control crossing and one 4-bit bus crossing."""
    _module, async_crossings, _spec = context
    assert len(async_crossings) == 2
    widths = sorted(c.width for c in async_crossings)
    assert widths == [1, 4]


def test_no_violations(context) -> None:
    """Structural gray-code recognition must accept this shape — both
    the gray-encode XOR pattern at the source and the multi-bit sync
    chain at the destination are present."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"unexpected violations on gray-counter fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
