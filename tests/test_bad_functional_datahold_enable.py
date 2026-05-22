"""Negative-case probe for the proposed CDC-012 — functional data-hold.

Issue #151 (signoff CDC coverage gaps).

The load request is synchronized through a 2FF chain before
controlling the destination bus capture, but the source payload
keeps advancing every src_clk cycle while the request is in flight.
The destination can therefore latch a payload from a later src
cycle than the one that motivated the original request — a
structurally-gated bus crossing that fails the functional
data-hold property.

xfail-strict until CDC-012 lands. The rule's PR should drop the
marker and tighten the assertion to "and only CDC-012 fires" plus
violation-shape checks, matching the convention used by the other
``test_bad_*`` files.
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


@pytest.mark.xfail(strict=True, reason="CDC-012 not yet implemented (issue #151)")
def test_cdc_012_fires(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert "CDC-012" in rule_ids
