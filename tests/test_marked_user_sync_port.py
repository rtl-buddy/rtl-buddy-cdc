"""User-declared synchronizer marker on an **output port** declaration.

Structural twin of ``marked_user_sync``: same single-flop shape, but
``(* cdc_sync *)`` is written on the ``output logic q_out`` declaration
rather than on a separate internal ``logic`` line. Yosys merges the
port and the underlying variable's attributes onto the same netname;
the slang frontend used to only read attributes off the
``VariableSymbol`` and silently dropped the port form — see issue #38.

Both frontends now agree, so this fixture exercises CDC-001
suppression via the port-attribute path under Yosys (the slang side
is pinned independently in ``test_slang_lowering.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules, user_sync_flop_names

FIX_DIR = Path(__file__).parent / "fixtures" / "marked_user_sync_port"
JSON = FIX_DIR / "marked_user_sync_port.json"
SDC = FIX_DIR / "marked_user_sync_port.sdc"


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


def test_port_attribute_lands_on_netname(context) -> None:
    """The `(* cdc_sync *)` attribute on the output port declaration
    must end up on ``module.netnames["q_out"].attributes``. This is
    the Yosys-side anchor for the fix to issue #38."""
    module, _crossings, _spec = context
    assert "cdc_sync" in module.netnames["q_out"].attributes


def test_user_sync_detected_via_port_attr(context) -> None:
    """The dst flop driving the `(* cdc_sync *)` output port should
    be in the user-sync set — the rule pack only looks at netname
    attributes, so the port-attribute path has to feed that bucket."""
    module, _crossings, _spec = context
    syncs = user_sync_flop_names(module)
    assert len(syncs) == 1


def test_no_violations_with_port_attribute(context) -> None:
    """Same shape as ``bad_single_ff_sync`` (single dst flop), so
    CDC-001 would normally fire; the port-level annotation must
    suppress it."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"unexpected violations on port-marked sync: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
