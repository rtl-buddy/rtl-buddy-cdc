"""Positive fixture for an ungated gray-coded bus crossing.

A 4-bit gray counter increments in src_clk; its value is sampled every
dst_clk cycle by a per-bit 2FF synchronizer with no handshake. Only the
gray encoding keeps the bus coherent. CDC-004's structural-gray
detector (rules.py `_is_multibit_sync_first_stage` paired with
`_is_gray_encoded_source`) must accept this shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "good_gray_bus_sync_chain"
JSON = FIX_DIR / "good_gray_bus_sync_chain.json"
SDC = FIX_DIR / "good_gray_bus_sync_chain.sdc"


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


def test_one_4bit_crossing(context) -> None:
    """A single 4-bit gray bus crosses src_clk → dst_clk."""
    _module, async_crossings, _spec = context
    assert len(async_crossings) == 1
    c = async_crossings[0]
    assert c.width == 4
    assert c.src_clock == "src_clk"
    assert c.dst_clock == "dst_clk"


def test_no_violations(context) -> None:
    """The structural-gray arm of CDC-004 must accept this shape — both
    the gray-encode XOR pattern at the source and the multi-bit sync
    chain at the destination are present, with no gating."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"unexpected violations on structural-gray fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
