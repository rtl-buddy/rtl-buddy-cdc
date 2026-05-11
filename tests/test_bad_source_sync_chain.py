"""Negative-case fixture: source-synchronous topology with a system
SDC that fails to declare the per-link clocks as related.

The topology is

    A ──► B0 ──► C0
     └──► B1 ──► C1

with each link captured directly flop-to-flop (no synchronizer —
correct for source-sync timing). When the system SDC declares all
five clocks independent and asynchronous to each other, the analyzer
must report each of the four direct flop-to-flop links as an
unsynchronized async crossing (CDC-001) — exactly the failure mode
this fixture exists to catch in system integration.

The paired ``good_source_sync_chain`` fixture uses the same RTL but
declares ck_b0/ck_b1/ck_c0/ck_c1 as ``create_generated_clock`` with a
master chain rooted at ck_a, modelling the source-sync relationship
correctly — and produces zero violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_source_sync_chain"
JSON = FIX_DIR / "bad_source_sync_chain.json"
SDC = FIX_DIR / "bad_source_sync_chain.sdc"

EXPECTED_LINKS = {
    ("ck_a", "ck_b0"),
    ("ck_a", "ck_b1"),
    ("ck_b0", "ck_c0"),
    ("ck_b1", "ck_c1"),
}


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings, spec


def test_four_async_crossings_one_per_source_sync_link(context) -> None:
    """All four datapath links show up as async crossings when the SDC
    misses the source-sync relationship."""
    _module, async_crossings, _spec = context
    pairs = {(c.src_clock, c.dst_clock) for c in async_crossings}
    assert pairs == EXPECTED_LINKS, (
        f"expected one async crossing per source-sync link, got {sorted(pairs)}"
    )


def test_cdc_001_fires_on_every_link(context) -> None:
    module, async_crossings, _spec = context
    violations = run_all_rules(module, async_crossings)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert len(cdc_001) == 4, (
        f"expected CDC-001 on each of the 4 source-sync links, "
        f"got {[(v.rule_id, v.message) for v in violations]}"
    )
    assert all(v.severity == "error" for v in cdc_001)


def test_good_sdc_resolves_all_clocks_to_ck_a(context) -> None:
    """Sanity check on the methodology: with the GOOD SDC, every
    block clock collapses to ck_a's master, so are_async returns
    False for every cross-block pair. This pins the SDC contract the
    good fixture relies on — drift in the parser would silently make
    the good fixture a no-op."""
    good_sdc = (
        Path(__file__).parent
        / "fixtures"
        / "good_source_sync_chain"
        / "good_source_sync_chain.sdc"
    )
    spec = sdc_mod.parse_file(good_sdc)
    clocks = ("ck_a", "ck_b0", "ck_b1", "ck_c0", "ck_c1")
    for c in clocks:
        assert spec.resolve(c) == "ck_a", f"{c} did not resolve to ck_a"
    for a, b in EXPECTED_LINKS:
        assert not spec.are_async(a, b), (
            f"{a}/{b} flagged async under the good SDC — "
            "create_generated_clock chain is broken"
        )
