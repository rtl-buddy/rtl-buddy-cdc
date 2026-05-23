"""Trust-boundary sentinel: `(* cdc_sync *)` on the SOURCE flop.

The attribute is wired onto the src-domain producer's netname; the
destination flop of the crossing has no attribute. CDC-001 suppression
is dst-flop-keyed (`c.dst_flop.cell.name in ctx.user_syncs`), so a
src-side marker is a no-op and CDC-001 must still fire on the depth-1
chain.

Pins the contract that `(* cdc_sync *)` annotates the *destination*
of a crossing, not the producer.

See issue #178.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_marked_user_sync_src_flop"
JSON = FIX_DIR / "bad_marked_user_sync_src_flop.json"
SDC = FIX_DIR / "bad_marked_user_sync_src_flop.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_001_fires_despite_src_side_marker(context) -> None:
    """The attribute on the source flop must not suppress CDC-001 on
    the (unmarked) destination flop."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert len(cdc_001) == 1, (
        f"expected exactly one CDC-001 finding, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
