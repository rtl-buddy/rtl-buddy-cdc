"""Negative-case fixture for G-5: CDC-001 / CDC-012 handshake-related tag."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_handshake_ack_missing"
JSON = FIX_DIR / "bad_handshake_ack_missing.json"
SDC = FIX_DIR / "bad_handshake_ack_missing.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_both_rules_fire(context) -> None:
    """The fixture is constructed so CDC-001 and CDC-012 both fire on
    the same async domain pair."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    by_rule = {v.rule_id for v in violations}
    assert {"CDC-001", "CDC-012"} <= by_rule, by_rule


def test_cdc_001_message_carries_handshake_tag(context) -> None:
    """The G-5 reporter refinement tags CDC-001 findings on the same
    domain pair as a CDC-012 finding."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert cdc_001
    tagged = [v for v in cdc_001 if "[handshake-related]" in v.message]
    assert tagged, "expected CDC-001 message to carry [handshake-related] tag"


def test_cdc_012_message_is_unchanged(context) -> None:
    """The tag is added to CDC-001 / CDC-002 only — the CDC-012
    message itself is untouched (it already explains the missing-ack
    failure mode)."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_012 = [v for v in violations if v.rule_id == "CDC-012"]
    assert cdc_012
    for v in cdc_012:
        assert "[handshake-related]" not in v.message
