"""User-declared synchronizer marker (`(* cdc_sync *)`).

The fixture uses a conventional 2FF synchronizer and annotates the
first stage. It exercises marker detection without relying on a
single-stage waiver that other CDC tools report as unsynchronized."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules, user_sync_flop_names

FIX_DIR = Path(__file__).parent / "fixtures" / "marked_user_sync"
JSON = FIX_DIR / "marked_user_sync.json"
SDC = FIX_DIR / "marked_user_sync.sdc"


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
    """The dst flop driving the `(* cdc_sync *)` wire should be in
    the user-sync set."""
    module, _crossings, _spec = context
    syncs = user_sync_flop_names(module)
    # There should be exactly one user-marked synchronizer flop in
    # this fixture. Its name is whatever yosys assigned; we only
    # assert the count.
    assert len(syncs) == 1


def test_no_violations_with_attribute(context) -> None:
    """The marked conventional 2FF synchronizer should stay clean."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"unexpected violations on user-marked sync: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_attribute_aliases() -> None:
    """The detector accepts a small set of aliases — sanity check
    the constant is plural and includes the canonical names."""
    from rtl_buddy_cdc.rules import USER_SYNC_ATTRS

    assert "cdc_sync" in USER_SYNC_ATTRS
    assert "synchronizer" in USER_SYNC_ATTRS
    # common synthesis-attribute alias for projects already using `(* ASYNC_REG *)`.
    assert "async_reg" in USER_SYNC_ATTRS
