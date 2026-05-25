"""Negative-case fixture for CDC-020: sliced multi-bit bus with per-lane syncs."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_sliced_bus_reconvergence"
JSON = FIX_DIR / "bad_sliced_bus_reconvergence.json"
SDC = FIX_DIR / "bad_sliced_bus_reconvergence.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_cdc_020_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_020 = [v for v in violations if v.rule_id == "CDC-020"]
    assert len(cdc_020) == 1, [v.rule_id for v in violations]


def test_cdc_020_lists_all_four_lanes(context) -> None:
    """The message should report 4 lanes — every bit of the sliced bus."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_020 = next(v for v in violations if v.rule_id == "CDC-020")
    assert "4 per-lane crossings" in cdc_020.message
