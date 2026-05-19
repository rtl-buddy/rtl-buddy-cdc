"""Phase-3 fixture for CDC-010 (#135): tech-mapped clock mux.

Same failure shape as ``bad_async_clock_mux`` (#134), but the
fixture is built with ``simplemap t:$mux`` so the high-level
``$mux`` lowers to a gate-level ``$_MUX_``. The rule must still
find the ``$_MUX_.S`` control pin via the extended explicit map
and fire exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import _filter_async
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_techmap_async_clock_mux"
JSON = FIX_DIR / "bad_techmap_async_clock_mux.json"
SDC = FIX_DIR / "bad_techmap_async_clock_mux.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_crossings = _filter_async(crossings, spec)
    return module, async_crossings, spec


def test_fixture_contains_techmapped_mux(context) -> None:
    """Guard the fixture's build script: if ``simplemap t:$mux`` ever
    stops lowering the ternary to ``$_MUX_`` we'd silently fall back
    to testing the phase-1 ``$mux`` path."""
    module, _, _ = context
    types = {c.type for c in module.cells.values()}
    assert "$_MUX_" in types, sorted(types)
    assert "$mux" not in types


def test_cdc_010_fires_on_techmapped_mux(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    assert len(cdc_010) == 1
    assert cdc_010[0].severity == "error"
    assert "$_MUX_" in cdc_010[0].message
    assert "control pin S" in cdc_010[0].message
    assert "ck1" in cdc_010[0].message
    assert "ck0" in cdc_010[0].message


def test_no_other_rule_fires_on_techmapped_mux(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert sorted({v.rule_id for v in violations}) == ["CDC-010"]


def test_heuristic_off_still_fires_via_explicit_map(context) -> None:
    """``$_MUX_`` is in the explicit map, so the heuristic flag is a
    no-op here — the rule fires either way. The opt-out flag exists
    to silence the *heuristic* path, not the gate-level mux family."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec, cdc_010_heuristic=False)
    assert [v.rule_id for v in violations] == ["CDC-010"]
