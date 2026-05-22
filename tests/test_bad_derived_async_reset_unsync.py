"""Negative-case fixture for RDC-006 — muxed async reset without local sync.

Issue #151 (signoff CDC coverage gaps).

A reset source selected through a $mux feeds the consumer flop's
async clear directly, with no local reset synchroniser in the
consumer clock domain. RDC-005 stays silent by design — the mux
makes selection unambiguous — but the selected reset's deassertion
edge is still asynchronous to ``clk``. RDC-006 fills the gap.
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


def test_rdc_006_fires_as_warning(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rdc_006 = [v for v in violations if v.rule_id == "RDC-006"]
    assert len(rdc_006) == 1, (
        f"expected exactly one RDC-006, got {[v.rule_id for v in violations]}"
    )
    v = rdc_006[0]
    assert v.severity == "warning"
    assert "muxed async reset without local synchroniser" in v.message
    assert "$mux" in v.message
    assert "block_rst_n" in v.message and "global_rst_n" in v.message
    # The select signal is a control, not a reset source — it must
    # NOT be reported as one in the message.
    assert "use_block_rst" not in v.message
    assert "2FF reset synchroniser" in v.message  # fix advice


def test_no_other_rules_fire(context) -> None:
    """Single-clock design, mux exemption keeps RDC-005 silent,
    no foreign-domain flops in the ARST path so RDC-001 is silent
    too. Only RDC-006."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"RDC-006"}, f"unexpected rules fired: {rule_ids}"
