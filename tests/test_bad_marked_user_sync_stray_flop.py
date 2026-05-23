"""Trust-boundary sentinel: `(* cdc_sync *)` on a stray (off-path) flop.

The attribute is wired onto a flop unrelated to the actual depth-1
crossing — a regular same-domain data register that the user has
mistakenly labelled. CDC-001 keys suppression by the offending
crossing's destination flop; the marked flop is not that flop, so the
attribute is a no-op and CDC-001 still fires on the real crossing.

Pins the contract that the attribute applies precisely to the flop
it lands on, with no spillover to other crossings in the design.

See issue #178.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules, user_sync_flop_names

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_marked_user_sync_stray_flop"
JSON = FIX_DIR / "bad_marked_user_sync_stray_flop.json"
SDC = FIX_DIR / "bad_marked_user_sync_stray_flop.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_marker_detected_on_stray_flop(context) -> None:
    """The decoy flop is recognised as marked — confirms the attribute
    plumbing works (so the rule-firing assertion below is meaningful)."""
    module, _crossings, _spec = context
    syncs = user_sync_flop_names(module)
    assert len(syncs) == 1, (
        f"expected exactly one (* cdc_sync *)-marked flop in the fixture, "
        f"got {sorted(syncs)}"
    )


def test_cdc_001_fires_on_unmarked_crossing(context) -> None:
    """The marker is on a stray flop; the actual depth-1 crossing's
    destination flop is unmarked. CDC-001 must still fire."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert len(cdc_001) == 1, (
        f"expected exactly one CDC-001 finding on the unmarked crossing, "
        f"got {[(v.rule_id, v.message) for v in violations]}"
    )
