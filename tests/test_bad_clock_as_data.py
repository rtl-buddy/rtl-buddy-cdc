"""Negative-case fixture: clock signals routed as data → CDC-008."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_clock_as_data"
JSON = FIX_DIR / "bad_clock_as_data.json"
SDC = FIX_DIR / "bad_clock_as_data.sdc"


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


def test_cdc_008_fires_three_times(context) -> None:
    """Three distinct clock-as-data sites: clk_a on the AND, clk_b on
    the AND, and clk_a on the dst flop's D."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_008 = [v for v in violations if v.rule_id == "CDC-008"]
    assert len(cdc_008) == 3
    assert all(v.severity == "error" for v in cdc_008)
    assert all("clock signal" in v.message for v in cdc_008)


def test_both_clocks_named(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_008 = [v for v in violations if v.rule_id == "CDC-008"]
    text = "\n".join(v.message for v in cdc_008)
    assert "clk_a" in text
    assert "clk_b" in text


def test_d_pin_misuse_flagged(context) -> None:
    """The flop D-pin misuse is the most surprising visually; ensure
    it's reported with the D pin in the message."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    d_pin_hits = [
        v for v in violations if v.rule_id == "CDC-008" and ".D[" in v.message
    ]
    assert len(d_pin_hits) == 1
