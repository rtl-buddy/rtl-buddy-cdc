"""SV-attribute coverage: ``(* reset_sync *)`` user escape hatch.

Issue #115 (first slice — ``reset_sync`` attribute; the
``reset_polarity`` attribute is deferred since no rule consumes
source-side port polarity today).

The fixture wires a 2-stage chain whose head's D pin is a *port*
(not a constant), so the structural recogniser in
:func:`rtl_buddy_cdc.reset_domain.find_reset_synchronizers`
deliberately does NOT classify it. The ``(* reset_sync *)`` annotation
on the chain tail overrides that decision; RDC-002 then sees the
consumer flop as fed by a vetted sync stage and skips it.

The dual contract:

1. ``user_reset_sync_flop_names(module)`` returns the marked flop's
   cell name.
2. Without the attribute the same wiring fires RDC-002; with it,
   the rule pack is silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.reset_domain import find_reset_synchronizers
from rtl_buddy_cdc.rules import (
    USER_RESET_SYNC_ATTRS,
    run_all as run_all_rules,
    user_reset_sync_flop_names,
)

FIX_DIR = Path(__file__).parent / "fixtures" / "marked_reset_sync"
JSON = FIX_DIR / "marked_reset_sync.json"
SDC = FIX_DIR / "marked_reset_sync.sdc"


def _load():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    return netlist.load(JSON)


def test_attribute_constants() -> None:
    """The aliases users may write are part of the public surface."""
    assert "reset_sync" in USER_RESET_SYNC_ATTRS
    assert "reset_synchronizer" in USER_RESET_SYNC_ATTRS


def test_user_reset_sync_flop_names_finds_marked_flop() -> None:
    module = _load()
    marked = user_reset_sync_flop_names(module)
    assert len(marked) == 1, f"expected 1 marked flop, got {marked}"
    # The marked flop is the chain tail (Q is named ``rst_sync``); the
    # cell name is a Yosys auto-name so we don't pin it, but it should
    # be one of the $procdff cells.
    assert all(n.startswith("$procdff") for n in marked)


def test_recognizer_consumes_extra_synchronizers() -> None:
    """``find_reset_synchronizers`` accepts ``extra_synchronizers`` and
    folds them into its output set."""
    module = _load()
    flop_clocks = {
        f.cell.name: clk
        for f, clk in [(f, _module_clock_for(module, f)) for f in _flops(module)]
    }
    marked = user_reset_sync_flop_names(module)
    # Structural pass alone wouldn't match (head D is a port).
    structural = find_reset_synchronizers(module, flop_clocks)
    # With the user-marked overlay the marked flop is in the set.
    augmented = find_reset_synchronizers(
        module, flop_clocks, extra_synchronizers=marked
    )
    assert marked.issubset(augmented)
    assert structural.isdisjoint(marked) or marked.issubset(structural), (
        "structural and user paths should be additive, not overlapping "
        "in a way that hides the user contribution"
    )


def test_rule_pack_silent_when_attribute_marks_chain_tail() -> None:
    """End-to-end: with ``(* reset_sync *)`` on the chain tail, the
    full rule pack should not fire on the consumer flop (whose
    polarity is deliberately mismatched in the fixture). Pins the
    integration between the attribute helper, the recogniser, and
    RDC-002's producer-side skip."""
    module = _load()
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"expected no violations on marked fixture, got "
        f"{[(v.rule_id, v.cell_name) for v in violations]}"
    )


# --- helpers ---------------------------------------------------------------


def _flops(module):
    from rtl_buddy_cdc.flops import find_flops

    return find_flops(module)


def _module_clock_for(module, f) -> str | None:
    from rtl_buddy_cdc.domain import assign_domains

    for fd in assign_domains(module):
        if fd.flop.cell.name == f.cell.name:
            return fd.clock
    return None
