"""Negative-case fixture: typed input port reaches a flop directly,
no synchronizer chain. CDC-001 must fire on the port→flop crossing
that find_crossings now emits when ClockSpec.port_clock is supplied."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_port_no_sync"
JSON = FIX_DIR / "bad_port_no_sync.json"
SDC = FIX_DIR / "bad_port_no_sync.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def test_emits_port_crossing(context) -> None:
    """find_crossings produces a port-sourced Crossing record when
    port_clock is supplied — backward-incompatible for callers that
    relied on src_flop never being None."""
    _module, async_crossings, _spec = context
    assert len(async_crossings) == 1
    c = async_crossings[0]
    assert c.src_port == "d_in"
    assert c.src_flop is None
    assert c.is_port_sourced
    assert c.src_name == "port d_in"


def test_cdc_001_fires_on_port_crossing(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert len(cdc_001) == 1
    assert "src: port d_in" in cdc_001[0].message
