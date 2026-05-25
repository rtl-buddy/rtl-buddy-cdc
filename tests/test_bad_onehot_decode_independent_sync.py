"""Negative-case fixture for CDC-019: shared comb decoder feeding independent per-lane syncs."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_onehot_decode_independent_sync"
JSON = FIX_DIR / "bad_onehot_decode_independent_sync.json"
SDC = FIX_DIR / "bad_onehot_decode_independent_sync.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_cdc_019_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_019 = [v for v in violations if v.rule_id == "CDC-019"]
    assert len(cdc_019) == 1, [v.rule_id for v in violations]


def test_cdc_019_lists_all_four_lanes(context) -> None:
    """The message should report 4 lanes — every bit of the decoded one-hot."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_019 = next(v for v in violations if v.rule_id == "CDC-019")
    assert "4 separate WIDTH=1" in cdc_019.message
