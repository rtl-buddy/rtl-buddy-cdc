"""Negative-case fixture: comb case-mux between src flops and sync.

Mirrors :mod:`test_bad_comb_before_sync` but the comb is an
``always_comb case`` rather than a continuous assign. Drives the
slang frontend's CaseStatement → chained-``$mux`` lowering (#37).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_comb_case_before_sync"
JSON = FIX_DIR / "bad_comb_case_before_sync.json"
SDC = FIX_DIR / "bad_comb_case_before_sync.sdc"


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


def test_three_async_crossings_through_case_mux(context) -> None:
    """All three src flops fan into the sync first stage through the
    case-mux chain, so we expect three crossings each with
    ``min_hops>=1``."""
    _module, async_crossings = context
    assert len(async_crossings) == 3
    assert all(c.min_hops >= 1 for c in async_crossings)
    assert all(c.width == 1 for c in async_crossings)


def test_cdc_003_fires(context) -> None:
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    cdc_003 = [v for v in violations if v.rule_id == "CDC-003"]
    assert len(cdc_003) == 3
    assert all(
        "combinational logic between source flop and synchronizer" in v.message
        for v in cdc_003
    )
    assert all(v.severity == "error" for v in cdc_003)


def test_no_cdc_001_fires(context) -> None:
    module, async_crossings = context
    violations = run_all_rules(module, async_crossings)
    assert [v for v in violations if v.rule_id == "CDC-001"] == []
