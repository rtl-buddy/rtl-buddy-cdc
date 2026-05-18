"""Unified ``find_reset_crossings`` API (issue #107).

The RDC rule family detects per-flop reset issues across five rules
(RDC-001..-005). External tooling (e.g. ``rtl-buddy-view``) wants the
same structural facts without re-implementing each rule's walk. This
test exercises ``find_reset_crossings`` against the existing RDC
fixtures to confirm it surfaces the same crossings the rule pack
would, classified by kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains
from rtl_buddy_cdc.reset_domain import find_reset_crossings, find_reset_synchronizers

FIX_ROOT = Path(__file__).parent / "fixtures"


def _load(fixture: str):
    name = fixture
    dir_ = FIX_ROOT / fixture
    json_path = dir_ / f"{name}.json"
    sdc_path = dir_ / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    flop_domains = assign_domains(module)
    clock_domains = {fd.flop.cell.name: fd.clock for fd in flop_domains}
    return module, clock_domains, spec


def test_bad_reset_crossing_emits_async_deassert() -> None:
    """``bad_reset_crossing`` is RDC-001's canonical bad case: an
    async reset crosses clock domains without a synchroniser.
    ``find_reset_crossings`` must emit at least one
    ``"async-deassert"`` for the consumer flop in the foreign
    domain."""
    module, clock_domains, _ = _load("bad_reset_crossing")
    syncs = find_reset_synchronizers(module, clock_domains)
    crossings = find_reset_crossings(module, clock_domains, recognised_syncs=syncs)
    kinds = {c.kind for c in crossings}
    assert "async-deassert" in kinds, (
        f"expected an async-deassert crossing, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_good_reset_sync_no_crossings() -> None:
    """``good_reset_sync`` has a proper reset-synchroniser chain.
    The recogniser's output, fed in as ``recognised_syncs``, must
    cause ``find_reset_crossings`` to skip every flop in the chain
    and return no crossings."""
    module, clock_domains, _ = _load("good_reset_sync")
    syncs = find_reset_synchronizers(module, clock_domains)
    crossings = find_reset_crossings(module, clock_domains, recognised_syncs=syncs)
    assert crossings == [], (
        f"expected no crossings on the good reset-sync fixture, got: "
        f"{[(c.flop, c.kind) for c in crossings]}"
    )


def test_bad_rdc_004_emits_comb_driven() -> None:
    """The RDC-004 bad fixture wires a reset through combinational
    logic. ``find_reset_crossings`` must classify that as
    ``"comb-driven"``."""
    module, clock_domains, _ = _load("bad_rdc_004_comb_driven_reset")
    crossings = find_reset_crossings(module, clock_domains)
    kinds = {c.kind for c in crossings}
    assert "comb-driven" in kinds, (
        f"expected a comb-driven crossing, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_bad_rdc_003_emits_sync_crossing() -> None:
    """The RDC-003 bad fixture has a sync reset crossing clock
    domains. ``find_reset_crossings`` must classify at least one
    such crossing as ``"sync-crossing"``."""
    module, clock_domains, _ = _load("bad_rdc_003_sync_reset_crossing")
    crossings = find_reset_crossings(module, clock_domains)
    kinds = {c.kind for c in crossings}
    assert "sync-crossing" in kinds, (
        f"expected a sync-crossing crossing, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_polarity_override_emits_polarity_mismatch() -> None:
    """Supplying a ``polarity_overrides`` map that disagrees with a
    flop's inferred polarity must emit a ``"polarity-mismatch"``
    crossing — independent of any crossing-kind that would also
    fire."""
    module, clock_domains, _ = _load("bad_marked_reset_polarity")
    crossings = find_reset_crossings(
        module,
        clock_domains,
        polarity_overrides={"rst_n": "low"},
    )
    kinds = {c.kind for c in crossings}
    assert "polarity-mismatch" in kinds, (
        f"expected polarity-mismatch, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_polarity_override_quiet_when_matching() -> None:
    """A matching polarity declaration must NOT emit a mismatch."""
    module, clock_domains, _ = _load("good_marked_reset_polarity")
    crossings = find_reset_crossings(
        module,
        clock_domains,
        polarity_overrides={"rst_n": "low"},
    )
    assert all(c.kind != "polarity-mismatch" for c in crossings), (
        f"unexpected polarity-mismatch on matching fixture: "
        f"{[(c.flop, c.kind) for c in crossings]}"
    )
