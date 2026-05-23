"""Sentinel fixture: raw-AND clock gate with foreign-domain enable.

The enable lives in `clk_b`'s domain; the AND output drives a flop's
CLK on `clk_a`'s branch. The clocks are declared async. Every enable
transition can chop the gated clock into runt pulses — exactly the
CDC-010 failure mode, but on a raw `$and` gate instead of a named
control pin.

CDC-010 today targets named-control-pin cells (`$mux.S`, `$dffe.EN`,
`$dlatch.EN`) and the pin-name heuristic for tech-mapped library
cells (`E` / `EN` / `CE` / `GATE` / `SE`). A bare `$and` cell falls
through every step — the rule treats AND/OR fanout as data-shape,
not control-shape, on the principle that the cell's inputs are
structurally symmetric. So this hazard is currently invisible to the
rule pack.

This test pins that gap. The expectation surfaced in issue #177 is
that future CDC-010 work (or a dedicated rule) will fire here; when
that lands, this test signals the coverage expansion and should be
updated to assert the new finding.

See issue #177.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_clock_gating_async_enable"
JSON = FIX_DIR / "bad_clock_gating_async_enable.json"
SDC = FIX_DIR / "bad_clock_gating_async_enable.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_raw_and_clock_gate_not_caught_today(context) -> None:
    """Foreign-domain enable into a raw-AND clock gate: real CDC
    hazard, currently invisible to the rule pack. Pin that gap as a
    regression sentinel — when CDC-010 (or a successor rule) grows
    coverage for raw `$and` clock-network cells, this test signals
    the change."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_010_findings = [v for v in violations if v.rule_id == "CDC-010"]
    assert cdc_010_findings == [], (
        f"CDC-010 now fires on a raw-AND clock gate with a "
        f"foreign-domain enable: {[v.message for v in cdc_010_findings]}. "
        f"Issue #177 anticipated this expansion — update this test to "
        f"assert the new finding (and add a paired good_* fixture if "
        f"none exists yet)."
    )
