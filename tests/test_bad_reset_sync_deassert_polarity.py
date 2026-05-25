"""Negative-case fixture for RDC-007: reset-sync chain with head D on the asserted polarity."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_reset_sync_deassert_polarity"
JSON = FIX_DIR / "bad_reset_sync_deassert_polarity.json"
SDC = FIX_DIR / "bad_reset_sync_deassert_polarity.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_rdc_007_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rdc_007 = [v for v in violations if v.rule_id == "RDC-007"]
    assert len(rdc_007) == 1, [v.rule_id for v in violations]


def test_only_rdc_007_fires(context) -> None:
    """The chain is structurally well-formed by every other check;
    the only failure mode is the deassertion-polarity inversion."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    fired = sorted({v.rule_id for v in violations})
    assert fired == ["RDC-007"], fired
