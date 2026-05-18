"""Negative-case fixture for RDC-004 — comb-driven reset pin.

Issue #114 (RDC-002 through RDC-005 rule pack), third sub-PR.

Two per-channel kill flops produce their Q outputs, which are
AND'd combinationally and used directly as the consumer flop's
ARST. The AND gate can glitch when the two Q outputs transition
near-simultaneously — and on an async-reset pin a transient looks
indistinguishable from a real assertion.

RDC-004 must fire once on the consumer. No other rule should fire
(single clock so RDC-001/003 silent; both polarities matched so
RDC-002 silent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_rdc_004_comb_driven_reset"
JSON = FIX_DIR / "bad_rdc_004_comb_driven_reset.json"
SDC = FIX_DIR / "bad_rdc_004_comb_driven_reset.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def test_rdc_004_fires_once(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rdc_004 = [v for v in violations if v.rule_id == "RDC-004"]
    assert len(rdc_004) == 1, (
        f"expected exactly one RDC-004, got {[v.rule_id for v in violations]}"
    )
    v = rdc_004[0]
    assert v.severity == "error"
    assert "combinational logic" in v.message
    assert "glitch" in v.message
    assert "Register the comb output" in v.message
    # The two upstream kill-channel flops must appear in the message.
    assert v.message.count("$procdff$") >= 2, (
        f"expected upstream flop names in message, got: {v.message}"
    )


def test_no_other_rules_fire(context) -> None:
    """Single-clock design with matched polarities — only RDC-004."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"RDC-004"}, f"unexpected rules fired: {rule_ids}"
