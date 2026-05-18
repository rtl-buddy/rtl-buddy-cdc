"""Negative-case fixture for RDC-002 — reset polarity mismatch.

Issue #114 (RDC-002 through RDC-005 rule pack), first sub-PR.

The fixture wires a single-bit reset path through a flop where the
producer's ``ARST_VALUE`` doesn't match the consumer's
``ARST_POLARITY``: the consumer can never enter reset when the
producer does — a classic polarity wiring bug.

RDC-002 must fire once on the consumer flop; no other rule should
fire (no clock crossing, no data path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_rdc_002_polarity_mismatch"
JSON = FIX_DIR / "bad_rdc_002_polarity_mismatch.json"
SDC = FIX_DIR / "bad_rdc_002_polarity_mismatch.sdc"


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


def test_rdc_002_fires_once(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert len(rdc_002) == 1, (
        f"expected exactly one RDC-002, got {[v.rule_id for v in violations]}"
    )
    v = rdc_002[0]
    assert v.severity == "error"
    # Message anchors that document the failure mode for the reader.
    assert "reset polarity mismatch" in v.message
    assert "ARST_POLARITY" in v.message
    assert "ARST_VALUE" in v.message
    # The cell anchor must be the consumer (the flop with the
    # mismatched polarity), not the producer — that's the flop the
    # user has to fix.
    assert v.cell_name is not None
    assert v.cell_name.startswith("$procdff")


def test_no_other_rules_fire(context) -> None:
    """Single-clock design with a reset polarity wiring bug — the only
    violation should be RDC-002."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"RDC-002"}, f"unexpected rules fired: {rule_ids}"
