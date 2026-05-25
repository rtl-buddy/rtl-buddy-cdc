"""Negative-case fixture for CDC-018: cascaded synchroniser chain."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_cascaded_synchroniser"
JSON = FIX_DIR / "bad_cascaded_synchroniser.json"
SDC = FIX_DIR / "bad_cascaded_synchroniser.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_cdc_018_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_018 = [v for v in violations if v.rule_id == "CDC-018"]
    assert len(cdc_018) == 1, [v.rule_id for v in violations]


def test_cdc_018_reports_depth_4(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_018 = next(v for v in violations if v.rule_id == "CDC-018")
    assert "4-flop chain" in cdc_018.message


def test_threshold_above_chain_silences(context) -> None:
    """Raising the threshold to 5 silences CDC-018 on a 4-flop chain."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec, cdc_018_depth_threshold=5)
    cdc_018 = [v for v in violations if v.rule_id == "CDC-018"]
    assert cdc_018 == []
