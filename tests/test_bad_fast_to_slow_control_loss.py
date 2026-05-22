"""Negative-case probe for the proposed CDC-013 — fast-to-slow event loss.

Issue #151 (signoff CDC coverage gaps).

A fast-domain toggle is sampled by a conventional 2FF synchronizer
in a slower destination domain. If two source events occur between
destination samples, the toggle returns to its prior value and the
destination observes zero edges. The synchronizer is structurally
safe for metastability; the failure is event accounting / protocol
loss, not metastability.

xfail-strict until CDC-013 lands. The rule's PR should drop the
marker and tighten the assertion to "and only CDC-013 fires" plus
violation-shape checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_fast_to_slow_control_loss"
JSON = FIX_DIR / "bad_fast_to_slow_control_loss.json"
SDC = FIX_DIR / "bad_fast_to_slow_control_loss.sdc"


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


@pytest.mark.xfail(strict=True, reason="CDC-013 not yet implemented (issue #151)")
def test_cdc_013_fires(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert "CDC-013" in rule_ids
