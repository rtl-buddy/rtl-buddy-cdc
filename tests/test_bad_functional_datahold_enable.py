"""Negative-case fixture for CDC-012 — functional data-hold on a gated bus.

Issue #151 (signoff CDC coverage gaps).

A multi-bit gated-bus crossing whose source payload updates every
src_clk cycle while the request travels through the 2FF
synchroniser. The structural shape is CDC-004-clean — the
destination is a $dffe whose EN is the synced request — but the
payload latched on the destination side may not be the one that
motivated the original request because the source kept advancing
the payload in the meantime. The textbook fix is a req/ack
handshake (source holds the payload until a synced-back ack
confirms the destination has sampled).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_functional_datahold_enable"
JSON = FIX_DIR / "bad_functional_datahold_enable.json"
SDC = FIX_DIR / "bad_functional_datahold_enable.sdc"


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


def test_cdc_012_fires_as_warning(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_012 = [v for v in violations if v.rule_id == "CDC-012"]
    assert len(cdc_012) == 1, (
        f"expected exactly one CDC-012, got {[v.rule_id for v in violations]}"
    )
    v = cdc_012[0]
    assert v.severity == "warning"
    assert "functional data-hold risk" in v.message
    assert "src_clk" in v.message and "dst_clk" in v.message
    assert "8-bit gated bus" in v.message
    assert "req/ack handshake" in v.message  # fix advice
    assert v.crossing is not None
    assert v.crossing.width == 8


def test_no_other_rules_fire(context) -> None:
    """CDC-004's gated-bus exemption is satisfied (dst is $dffe with
    EN sourced from a dst-domain 2FF sync), and the 1-bit req sync
    chain keeps CDC-001/-002/-003 silent. Only CDC-012 fires."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"CDC-012"}, f"unexpected rules fired: {rule_ids}"
