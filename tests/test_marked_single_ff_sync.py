"""User-declared synchronizer marker on a single-stage destination flop.

The fixture is structurally identical to `bad_single_ff_sync` — one src
flop, one dst flop, no second stage — but the dst flop's netname carries
`(* cdc_sync = "level_2ff" *)`. CDC-001/-002/-003 must honour the
marker and skip the flop, even though the structural depth is 1.

This complements `test_marked_user_sync.py`, which marks the first stage
of a conventional 2FF chain (a shape that would already pass without
the attribute)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules, user_sync_flop_names

FIX_DIR = Path(__file__).parent / "fixtures" / "marked_single_ff_sync"
JSON = FIX_DIR / "marked_single_ff_sync.json"
SDC = FIX_DIR / "marked_single_ff_sync.sdc"


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


def test_user_sync_detected(context) -> None:
    """The single annotated dst flop is the only user-marked sync."""
    module, _crossings, _spec = context
    syncs = user_sync_flop_names(module)
    assert len(syncs) == 1


def test_marker_suppresses_cdc_001_on_single_stage(context) -> None:
    """The same shape fires CDC-001 in `bad_single_ff_sync`; the
    `(* cdc_sync *)` annotation must suppress it here. This is the
    load-bearing case for the attribute — a real 2FF chain wouldn't
    need the marker at all."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"unexpected violations on marked single-stage sync: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
