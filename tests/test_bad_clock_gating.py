"""Sentinel fixture: raw-AND clock gate with combinational enable.

The design has no clock-domain crossing — it's a synthesis-correctness
hazard (use an ICG cell, not a raw `$and` gate). rtl-buddy-cdc is a
CDC analyzer, so the rule pack stays silent here today.

This test pins that behaviour. If the rule pack ever grows a glitch
detector that fires on this shape, the test surfaces the change so the
"glitch detection is out of scope for a CDC linter" boundary can be
re-evaluated deliberately.

See issue #177.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_clock_gating"
JSON = FIX_DIR / "bad_clock_gating.json"
SDC = FIX_DIR / "bad_clock_gating.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_no_violations_today(context) -> None:
    """Single-domain raw-AND clock gate: glitch hazard, not a CDC
    one. No rules fire today; pin that behaviour."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        "regression: rules now fire on a same-domain raw-AND clock gate "
        f"({[(v.rule_id, v.message) for v in violations]}). The fixture "
        "documents a glitch hazard that has historically been outside "
        "this analyzer's CDC remit; if the rule pack has grown a glitch "
        "detector, update the issue #177 thread and adjust this test."
    )
