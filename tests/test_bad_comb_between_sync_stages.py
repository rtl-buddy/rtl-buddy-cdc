"""CDC-014 — comb logic between synchroniser stages (issue #171).

The fixture wires a 2FF synchroniser chain with an AND gate between
``sync_1.Q`` and ``sync_2.D``. The chain walker terminates at
depth = 1 (sync_1.Q drives the AND, not a flop's D directly), so the
old CDC-001 message ``"no second-stage synchronizer"`` would
mislead the user — a second stage *is* there, just behind a gate.
CDC-001 defers via ``_chain_has_inter_stage_comb``, and CDC-014 fires
with the correct framing.

Also pins that the deferral doesn't accidentally suppress CDC-001 on
the existing single-flop bad fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_comb_between_sync_stages"
JSON = FIX_DIR / "bad_comb_between_sync_stages.json"
SDC = FIX_DIR / "bad_comb_between_sync_stages.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_014_fires(context) -> None:
    """Exactly one CDC-014 finding on the inter-stage gate."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_014 = [v for v in violations if v.rule_id == "CDC-014"]
    assert len(cdc_014) == 1, (
        f"expected exactly one CDC-014 finding, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_cdc_001_deferred(context) -> None:
    """CDC-001 must NOT fire — its deferral path takes over when a
    follow-on flop is present behind a gate, since the
    'no second-stage' message would mislead the user."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert cdc_001 == [], (
        f"CDC-001 should defer to CDC-014 when inter-stage comb is "
        f"present, but got {[(v.rule_id, v.message) for v in cdc_001]}"
    )


def test_existing_single_ff_still_fires_cdc_001() -> None:
    """Regression sentinel: the existing single-flop bad fixtures
    must not be accidentally suppressed by the new CDC-001 deferral.
    The deferral only triggers when a follow-on flop exists behind a
    gate — `bad_single_ff_sync` has no follow-on flop at all."""
    fixtures = [
        "bad_single_ff_sync",
        "bad_port_no_sync",
    ]
    fix_root = Path(__file__).parent / "fixtures"
    for name in fixtures:
        json_path = fix_root / name / f"{name}.json"
        sdc_path = fix_root / name / f"{name}.sdc"
        if not json_path.exists():
            pytest.skip(f"fixture not built: {json_path}")
        module = netlist.load(json_path)
        spec = sdc_mod.parse_file(sdc_path)
        crossings = find_crossings(module, port_clock=spec.port_clock)
        violations = run_all_rules(module, crossings, spec)
        cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
        assert len(cdc_001) >= 1, (
            f"{name}: CDC-001 must still fire (no inter-stage comb to defer to). "
            f"Got: {[(v.rule_id, v.message) for v in violations]}"
        )
