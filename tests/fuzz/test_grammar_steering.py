"""Coverage-steering unit tests for the Stage-4 grammar fuzzer.

Pure-Python tests — no Yosys, no analyzer. The steering picker is
a static function over :data:`tests.fuzz.grammar.PRODUCTIONS` and
the declared verdict on each production; these tests pin its
contract independently of the elaboration pipeline so a steering
regression surfaces in the no-yosys CI job too.
"""

from __future__ import annotations

import pytest

from .grammar import PRODUCTIONS, productions_lifting, under_covered_rules

pytestmark = pytest.mark.fuzz_grammar


def test_productions_lifting_picks_only_declared_targets() -> None:
    """A production is included iff its declared ``cdc_rules_added``
    intersects the requested rule set."""
    picks = productions_lifting({"CDC-001"})
    assert picks, "expected at least one production to declare CDC-001"
    for p in picks:
        assert "CDC-001" in p.declared.cdc_rules_added

    # The clean-chain production declares no added rules — it must
    # never appear in any lifting set, regardless of which rule we
    # ask for.
    clean = next(p for p in PRODUCTIONS if p.name == "clean_sync_chain")
    assert clean not in productions_lifting({"CDC-001"})
    assert clean not in productions_lifting({"CDC-006", "RDC-001"})


def test_productions_lifting_preserves_registry_order() -> None:
    """Order is deterministic — the picker preserves the input
    registry's order so consumers can rely on the bias-set being
    stable for a fixed PRODUCTIONS tuple."""
    target_rules = {"CDC-001", "CDC-006", "RDC-001"}
    picks = productions_lifting(target_rules)
    registry_index = {p.name: i for i, p in enumerate(PRODUCTIONS)}
    indices = [registry_index[p.name] for p in picks]
    assert indices == sorted(indices)


def test_under_covered_rules_threshold() -> None:
    fires = {"CDC-001": 0, "CDC-006": 5, "CDC-014": 12}
    # With threshold=10, CDC-001 + CDC-006 are under-covered.
    assert under_covered_rules(fires, threshold=10) == {"CDC-001", "CDC-006"}
    # With threshold=1, only CDC-001 (zero fires) is under-covered.
    assert under_covered_rules(fires, threshold=1) == {"CDC-001"}


def test_under_covered_rules_universe_surfaces_zero_fire_rules() -> None:
    """A rule absent from ``fires`` defaults to zero fires — without
    ``rule_universe``, that rule is invisible to the picker. With
    ``rule_universe`` containing it, it surfaces as under-covered."""
    fires: dict[str, int] = {"CDC-001": 99}
    universe = {"CDC-001", "CDC-002", "RDC-008"}
    # CDC-002 and RDC-008 default to 0 fires, both below threshold 10.
    assert under_covered_rules(fires, threshold=10, rule_universe=universe) == {
        "CDC-002",
        "RDC-008",
    }


def test_steering_round_trip() -> None:
    """End-to-end: from a fires-counter, identify the
    under-covered rules and ask the picker for productions that
    would lift them. The picker must return at least one production
    when the under-covered rule is declared by *any* production
    in the registry — the round-trip's actionable signal."""
    fires = {p: 0 for p in {"CDC-001", "CDC-006", "RDC-001"}}
    targets = under_covered_rules(fires, threshold=1)
    picks = productions_lifting(targets)
    assert picks, "registry has productions declaring CDC-001 / CDC-006 / RDC-001"
    declared = set()
    for p in picks:
        declared |= p.declared.cdc_rules_added
    # Every requested under-covered rule is declared by at least
    # one picked production — that's the contract steering relies on.
    assert targets.issubset(declared)
