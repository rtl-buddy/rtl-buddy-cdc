"""Negative-case fixture: unregistered comb source feeds a sync → CDC-006."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_comb_source"
JSON = FIX_DIR / "bad_comb_source.json"
SDC = FIX_DIR / "bad_comb_source.sdc"


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


def test_no_flop_to_flop_crossings(context) -> None:
    """No source-domain flop exists, so the structural crossing pass
    cannot see this; CDC-006 has to come from a separate iteration."""
    _module, async_crossings, _spec = context
    assert async_crossings == []


def test_cdc_006_fires(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_006 = [v for v in violations if v.rule_id == "CDC-006"]
    assert len(cdc_006) == 1
    v = cdc_006[0]
    assert "glitchy combinational source" in v.message
    # Both top-level inputs should be named in the report.
    assert "a" in v.message
    assert "b" in v.message
    assert v.severity == "error"


def test_no_cdc_001_fires(context) -> None:
    """CDC-001 fires only when an actual flop->flop crossing exists.
    Here there isn't one, so CDC-001 must stay silent."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert [v for v in violations if v.rule_id == "CDC-001"] == []
