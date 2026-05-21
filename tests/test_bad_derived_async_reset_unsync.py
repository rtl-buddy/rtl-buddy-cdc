"""Negative-case probe for the proposed RDC-006 — derived async reset.

Issue #151 (signoff CDC coverage gaps).

A reset source selected through a $mux feeds the consumer flop's
async clear directly, with no local reset synchronizer in the
consumer clock domain. RDC-005 stays silent by design (the mux
makes selection unambiguous), but reset deassertion is still
unaligned to clk — the gap RDC-006 is meant to cover.

Today this exact topology is also the body of
``good_rdc_005_muxed_reset``. When RDC-006 lands, that fixture will
need a downstream sync stage so it remains clean for both rules
(see issue #151 acceptance criteria).

xfail-strict until RDC-006 lands. The rule's PR should drop the
marker and tighten the assertion to "and only RDC-006 fires" plus
violation-shape checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_derived_async_reset_unsync"
JSON = FIX_DIR / "bad_derived_async_reset_unsync.json"
SDC = FIX_DIR / "bad_derived_async_reset_unsync.sdc"


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


@pytest.mark.xfail(strict=True, reason="RDC-006 not yet implemented (issue #151)")
def test_rdc_006_fires(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert "RDC-006" in rule_ids
