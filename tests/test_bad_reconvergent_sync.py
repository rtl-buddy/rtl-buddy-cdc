"""Negative-case fixture: one src flop fans into two sync chains → CDC-005."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_reconvergent_sync"
JSON = FIX_DIR / "bad_reconvergent_sync.json"
SDC = FIX_DIR / "bad_reconvergent_sync.sdc"


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


def test_two_crossings_share_source(context) -> None:
    """Both async crossings should originate from the same src flop."""
    _module, async_crossings, _spec = context
    assert len(async_crossings) == 2
    src_names = {c.src_flop.cell.name for c in async_crossings}
    assert len(src_names) == 1
    assert all(c.width == 1 for c in async_crossings)


def test_cdc_005_fires_once(context) -> None:
    """One CDC-005 violation summarising the reconvergent fan-out, not
    one-per-target — the rule reports per (src, dst_clock) pair."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_005 = [v for v in violations if v.rule_id == "CDC-005"]
    assert len(cdc_005) == 1
    v = cdc_005[0]
    assert "reconvergent synchronizers" in v.message
    assert "2 independent sync chains" in v.message
    assert v.severity == "warning"


def test_no_cdc_001_fires(context) -> None:
    """Both sync chains are 2 deep, so CDC-001 should not fire — the
    issue is the reconvergence, not absent synchronizers."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert [v for v in violations if v.rule_id == "CDC-001"] == []
