"""Negative-case fixture: one async-reset source flop in src_clk feeds
ARST on four flops in dst_clk. CDC-007 should fire ONCE for the
shared source (not four times) and the message should list multiple
destination flops."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_reset_tree"
JSON = FIX_DIR / "bad_reset_tree.json"
SDC = FIX_DIR / "bad_reset_tree.sdc"


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
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def test_cdc_007_fires_once_for_shared_source(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_007 = [v for v in violations if v.rule_id == "CDC-007"]
    assert len(cdc_007) == 1, (
        f"expected 1 grouped CDC-007, got {len(cdc_007)}: "
        f"{[v.message for v in cdc_007]}"
    )
    msg = cdc_007[0].message
    assert "4 destination flops share this source" in msg
    assert "reset distribution tree" in msg
