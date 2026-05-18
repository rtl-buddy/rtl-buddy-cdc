"""Negative-case fixture for RDC-005 — multi-source reset convergence.

Issue #114 (RDC-002 through RDC-005 rule pack), fourth and final
sub-PR.

A flop's ARST is the AND of two independent top-level reset ports.
Both sources are simultaneously active; there's no $mux / control
signal making the selection explicit. RDC-005 fires as a `warning`
on the consumer; no other rule should fire (the comb fanin contains
no flops, so RDC-004 deliberately skips this pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_rdc_005_multi_source_reset"
JSON = FIX_DIR / "bad_rdc_005_multi_source_reset.json"
SDC = FIX_DIR / "bad_rdc_005_multi_source_reset.sdc"


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


def test_rdc_005_fires_as_warning(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rdc_005 = [v for v in violations if v.rule_id == "RDC-005"]
    assert len(rdc_005) == 1, (
        f"expected exactly one RDC-005, got {[v.rule_id for v in violations]}"
    )
    v = rdc_005[0]
    assert v.severity == "warning"
    assert "multiple reset sources converging" in v.message
    assert "global_rst_n" in v.message and "block_rst_n" in v.message
    assert "$mux" in v.message  # the fix advice mentions muxing


def test_no_other_rules_fire(context) -> None:
    """Single-clock design, comb fanin has no flops → RDC-004 silent.
    Both polarities matched → RDC-002 silent. Only RDC-005."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"RDC-005"}, f"unexpected rules fired: {rule_ids}"
