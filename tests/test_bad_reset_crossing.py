"""Negative-case fixture: src-domain flop drives dst flop's ARST → RDC-001."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_reset_crossing"
JSON = FIX_DIR / "bad_reset_crossing.json"
SDC = FIX_DIR / "bad_reset_crossing.sdc"


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


def test_no_data_crossings(context) -> None:
    """The only inter-domain signal is the reset itself, which doesn't
    appear as a flop->flop data crossing."""
    _module, async_crossings, _spec = context
    assert len(async_crossings) == 0


def test_rdc_001_fires(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rdc_001 = [v for v in violations if v.rule_id == "RDC-001"]
    assert len(rdc_001) == 1
    v = rdc_001[0]
    assert "async reset crossing" in v.message
    assert "src_clk" in v.message and "dst_clk" in v.message
    assert v.severity == "error"
    # RDC-001 doesn't carry a data crossing.
    assert v.crossing is None


def test_no_other_rules_fire(context) -> None:
    """No data crossings exist, so CDC-001..004 must all stay silent."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"RDC-001"}
