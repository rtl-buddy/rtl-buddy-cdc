"""Trust-boundary sentinel: `(* cdc_sync *)` on the WRONG stage.

A 2FF chain in dst_clk with the attribute on stage 2 instead of
stage 1. At the default `required_depth=2` the chain is structurally
clean and no rule fires either way — so this test runs with
`required_depth=3` to expose the wrong-stage placement: CDC-002 fires
on the (unmarked) head, and the (correctly-marked-but-wrong-flop)
stage 2 does not suppress it.

Pins the contract that `(* cdc_sync *)` annotates one specific flop
and does not retroactively whitelist the entire chain.

See issue #178.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_marked_user_sync_wrong_stage"
JSON = FIX_DIR / "bad_marked_user_sync_wrong_stage.json"
SDC = FIX_DIR / "bad_marked_user_sync_wrong_stage.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_clean_at_default_depth(context) -> None:
    """At `required_depth=2` the chain is structurally clean. No rule
    fires — the wrong-stage placement is invisible at this setting."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        f"expected no findings at default sync-depth, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_cdc_002_fires_at_depth_3(context) -> None:
    """At `required_depth=3` the chain is insufficient. CDC-002 fires
    on the head (stage 1, unmarked); the attribute on stage 2 does not
    apply because suppression is dst-flop-keyed."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec, required_depth=3)
    cdc_002 = [v for v in violations if v.rule_id == "CDC-002"]
    assert len(cdc_002) == 1, (
        f"expected exactly one CDC-002 finding at sync-depth 3, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
