"""Negative-case fixture: a port typed into foreign_clk via
set_input_delay reaches a synchronizer in dst_clk through pure comb.
Both ends of the path are typed, and the SDC declares the clocks
async, so CDC-006 must fire and the message should name the source
clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_input_delay_cross_domain"
JSON = FIX_DIR / "bad_input_delay_cross_domain.json"
SDC = FIX_DIR / "bad_input_delay_cross_domain.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def test_cdc_006_fires_with_source_clock_named(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_006 = [v for v in violations if v.rule_id == "CDC-006"]
    assert len(cdc_006) == 1
    v = cdc_006[0]
    # Both port names + the foreign clock name must appear in the
    # report so reviewers see *which* domain the comb source comes
    # from.
    assert "a (clock=foreign_clk)" in v.message
    assert "b (clock=foreign_clk)" in v.message
    assert v.severity == "error"
