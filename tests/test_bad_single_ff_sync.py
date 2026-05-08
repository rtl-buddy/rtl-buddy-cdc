"""Negative-case fixture: single-flop "synchronizer" → CDC-002 fires."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_single_ff_sync"
JSON = FIX_DIR / "bad_single_ff_sync.json"
SDC = FIX_DIR / "bad_single_ff_sync.sdc"


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


def test_cdc_002_fires(context) -> None:
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    cdc_002 = [v for v in violations if v.rule_id == "CDC-002"]
    assert len(cdc_002) == 1
    assert "insufficient synchronizer depth" in cdc_002[0].message
    assert cdc_002[0].severity == "error"
