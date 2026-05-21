"""Typed top-level bus crossings still need CDC-004 coherence checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_typed_port_bus_crossing"
JSON = FIX_DIR / "bad_typed_port_bus_crossing.json"
SDC = FIX_DIR / "bad_typed_port_bus_crossing.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings, spec


def test_typed_port_bus_crossing_is_visible(context) -> None:
    _module, async_crossings, _spec = context
    in_crossings = [c for c in async_crossings if c.src_port == "in"]
    assert len(in_crossings) == 1
    assert in_crossings[0].width == 8


def test_cdc_004_fires_for_typed_port_bus(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert {v.rule_id for v in violations} == {"CDC-004"}
    assert "port in" in violations[0].message
