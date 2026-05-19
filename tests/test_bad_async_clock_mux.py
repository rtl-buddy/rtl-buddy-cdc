"""Negative-case fixture for CDC-010 (#134, phase 2 of #95).

A clock mux whose ``S`` is driven by a flop in a foreign clock
domain. The rule must fire exactly once and only CDC-010 — the
shape is invisible to CDC-001 / CDC-004 (the select sits on a
``$mux``, not a flop's ``D``) and to CDC-008 (no clock signal lands
on a data pin here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import _filter_async
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_async_clock_mux"
JSON = FIX_DIR / "bad_async_clock_mux.json"
SDC = FIX_DIR / "bad_async_clock_mux.sdc"


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


def _sel_q_cell_name(module) -> str:
    """Yosys auto-names the always_ff flop ``$procdff$N``; recover the
    cell whose ``Q`` drives the ``sel_q`` netname so the test can
    assert on a stable identity instead of the unstable cell name."""
    sel_q = module.netnames["sel_q"]
    for cell in module.cells.values():
        if "$adff" not in cell.type and "$dff" not in cell.type:
            continue
        if cell.connections.get("Q", ()) == sel_q.bits:
            return cell.name
    raise AssertionError("no flop drives the sel_q netname")


def _mux_cell(module):
    muxes = [c for c in module.cells.values() if c.type == "$mux"]
    assert len(muxes) == 1, f"expected one $mux, got {len(muxes)}"
    return muxes[0]


def test_cdc_010_fires_exactly_once(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    assert len(cdc_010) == 1, [v.message for v in cdc_010]
    assert cdc_010[0].severity == "error"


def test_violation_names_the_mux_cell(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    mux = _mux_cell(module)
    assert cdc_010[0].cell_name == mux.name
    assert "$mux" in cdc_010[0].message
    assert "control pin S" in cdc_010[0].message


def test_violation_names_sel_q_as_source(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    sel_q = _sel_q_cell_name(module)
    assert sel_q in cdc_010[0].message, (
        f"sel_q cell {sel_q!r} missing from message: {cdc_010[0].message!r}"
    )
    # And the domains — sel_q is ck1, the gated clock is ck0.
    assert "ck1" in cdc_010[0].message
    assert "ck0" in cdc_010[0].message


def test_no_other_rule_fires(context) -> None:
    """CDC-001 / -004 can't see a select-on-$mux shape (it's not a
    flop D); CDC-008 must stay silent here (no clock signal sits on
    a non-CLK data pin). CDC-011 is suppressed by ``set_input_delay``
    in the SDC. CDC-010 should be the only rule that fires."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    assert rule_ids == ["CDC-010"], rule_ids
