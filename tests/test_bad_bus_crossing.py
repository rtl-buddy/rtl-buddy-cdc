"""Negative-case fixture: ungated 8-bit bus crossing → CDC-004."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_bus_crossing"
JSON = FIX_DIR / "bad_bus_crossing.json"
SDC = FIX_DIR / "bad_bus_crossing.sdc"


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


def test_one_8bit_crossing(context) -> None:
    _module, async_crossings = context
    assert len(async_crossings) == 1
    c = async_crossings[0]
    assert c.width == 8
    assert c.min_hops == 0  # direct flop-to-flop bus, no gating possible


def test_cdc_004_fires(context) -> None:
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) == 1
    v = cdc_004[0]
    assert "unprotected bus crossing" in v.message
    assert "8-bit path" in v.message
    assert v.severity == "error"


def test_no_other_rules_fire(context) -> None:
    """Width>1 means CDC-001/-002/-003 should all stay silent — they
    apply only to single-bit control crossings."""
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"CDC-004"}
