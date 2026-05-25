"""Negative-case fixture for CDC-021: flop CLK driven by undeclared port."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_flop_clk_undeclared_port"
JSON = FIX_DIR / "bad_flop_clk_undeclared_port.json"
SDC = FIX_DIR / "bad_flop_clk_undeclared_port.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_cdc_021_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_021 = [v for v in violations if v.rule_id == "CDC-021"]
    assert len(cdc_021) == 1, [v.rule_id for v in violations]


def test_only_cdc_021_fires(context) -> None:
    """The fixture is self-contained — no data port, no cross-domain crossing."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    fired = sorted({v.rule_id for v in violations})
    assert fired == ["CDC-021"], fired
