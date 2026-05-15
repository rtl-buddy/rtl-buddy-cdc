"""Negative-case fixture: two sync chains whose Qs recombine in a
downstream register → CDC-005.

Stronger version of ``bad_reconvergent_sync``: the recombination
point is itself a flop, not just a comb cell driving a port. Phase 2
of CDC-005 (issue #33) introduces a forward-cone reconvergence filter
that must still fire on this textbook shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_reconvergent_with_recombine"
JSON = FIX_DIR / "bad_reconvergent_with_recombine.json"
SDC = FIX_DIR / "bad_reconvergent_with_recombine.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings, spec


def test_two_crossings_share_source(context) -> None:
    """Both async crossings originate from the same source flop —
    the shape the rule keys on."""
    _module, async_crossings, _spec = context
    assert len(async_crossings) == 2
    src_names = {c.src_flop.cell.name for c in async_crossings}
    assert len(src_names) == 1


def test_cdc_005_fires_on_flop_recombination(context) -> None:
    """A flop downstream of both sync chains is reached by the
    phase-2 forward-cone walk; the recombination is real, so CDC-005
    must fire."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_005 = [v for v in violations if v.rule_id == "CDC-005"]
    assert len(cdc_005) == 1
    v = cdc_005[0]
    assert "reconvergent synchronizers" in v.message
    assert v.severity == "warning"
