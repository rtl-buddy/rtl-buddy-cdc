"""Negative-case fixture: single-flop "synchronizer" → CDC-001 fires."""

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


def test_cdc_001_fires(context) -> None:
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert len(cdc_001) == 1
    assert "unsynchronized control crossing" in cdc_001[0].message
    assert "no second-stage synchronizer" in cdc_001[0].message
    assert cdc_001[0].severity == "error"


def test_no_cdc_002_at_default_depth(context) -> None:
    """CDC-002 should be silent at the default required_depth = 2 — even
    though depth here is only 1, that's CDC-001 territory, not CDC-002."""
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    assert [v for v in violations if v.rule_id == "CDC-002"] == []
