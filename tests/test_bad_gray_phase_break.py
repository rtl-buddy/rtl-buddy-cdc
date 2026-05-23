"""Gray-coded value re-registered before the crossing (issue #174).

A Gray-coded counter feeds an intermediate registering stage in
`src_clk` before being sampled by a 2FF multi-bit synchroniser in
`dst_clk`. The intermediate flop breaks Gray's one-bit-flip
invariant: at any destination sample point the intermediate flop can
hold the previous Gray value while the counter has already advanced.

CDC-004's structural Gray detector
(:func:`rtl_buddy_cdc.rules._is_gray_encoded_source`) walks back from
the *crossing's* source flop and stops at the first Q-pin boundary —
the upstream Gray-XOR shape is invisible across an intermediate
register. So the detector correctly refuses to exempt this crossing
and CDC-004 fires.

This test pins that detector behaviour as a regression sentinel:
future tightening (or any change that walks across an intermediate
flop) would either silently exempt this anti-pattern (false
negative — test fails) or break it the other way (true positive
disappears — test fails).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_gray_phase_break"
JSON = FIX_DIR / "bad_gray_phase_break.json"
SDC = FIX_DIR / "bad_gray_phase_break.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_004_fires_on_intermediate_reregister(context) -> None:
    """The intermediate flop breaks Gray's one-bit-flip invariant.
    CDC-004 must fire — the structural Gray detector terminates at
    the upstream flop boundary and cannot match the XOR shape."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) == 1, (
        f"expected exactly one CDC-004 finding, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    assert "4-bit" in cdc_004[0].message, cdc_004[0].message


def test_only_cdc_004_fires(context) -> None:
    """No other rule should partially-suppress: a multi-bit unsynced
    crossing belongs to CDC-004 alone, not CDC-001/-002 (those handle
    single-bit chains) or CDC-005 (no reconvergent fanout here)."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    assert rule_ids == ["CDC-004"], f"expected CDC-004 only, got {rule_ids}"
