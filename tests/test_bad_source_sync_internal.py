"""Negative-case fixture: source-synchronous chain wired *internally*.

Same topology as ``bad_source_sync_chain``, but the forwarded clocks
exist only as internal nets (not top-level ports). The system SDC
declares each forwarded clock with ``create_generated_clock`` at the
internal pin where it originates *and* lists all five clocks as
pairwise asynchronous via ``set_clock_groups -asynchronous`` — the
methodology bug we want to surface.

The analyzer must:

  - parse the pin-targeted ``create_generated_clock`` declarations
    and stop ``trace_clock_root`` at each pin, giving every block a
    distinct clock identity (ck_a, ck_b0, ck_b1, ck_c0, ck_c1);
  - honour the unresolved-name async-groups override so each
    source-sync link is reported as a CDC-001 violation.

The paired ``good_source_sync_internal`` fixture uses the same RTL +
the same ``create_generated_clock`` chain but *without* the
``set_clock_groups -asynchronous`` line, and produces zero violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_source_sync_internal"
JSON = FIX_DIR / "bad_source_sync_internal.json"
SDC = FIX_DIR / "bad_source_sync_internal.sdc"

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
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings, spec


def test_internal_pin_generated_clocks_parsed(context) -> None:
    """The SDC parser populates ``pin_clocks`` for each pin-targeted
    ``create_generated_clock``."""
    _module, _crossings, spec = context
    assert spec.pin_clocks == {
        "u_a/clk_out_b0": "ck_b0",
        "u_a/clk_out_b1": "ck_b1",
        "u_b0/clk_out": "ck_c0",
        "u_b1/clk_out": "ck_c1",
    }


def test_four_async_crossings_one_per_source_sync_link(context) -> None:
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


def test_good_sdc_resolves_all_clocks_to_ck_a() -> None:
    """Sanity check: with the GOOD internal-pin SDC, every block clock
    collapses to ck_a's master, so are_async returns False for every
    cross-block pair. Pins the contract the good fixture relies on."""
    good_sdc = (
        Path(__file__).parent
        / "fixtures"
        / "good_source_sync_internal"
        / "good_source_sync_internal.sdc"
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
