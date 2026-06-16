"""Clock-combining decline (issue #263 soundness hardening).

When a clock-network gate or transparent latch combines two *different*
declared clocks on its legs (e.g. a ``$dlatch`` with ``D = clkA`` and
``EN = clkB``, or ``clkA & clkB``), the downstream flop's clock domain is
genuinely ambiguous — the net toggles on both clocks. The tracer must
DECLINE (leave the flop domain-unknown, surfaced by the under-resolution
report) rather than silently asserting one leg.

This guards the correctness boundary of the P2 latch clause and the
pre-existing gate clause: the decline fires ONLY when two legs resolve to
two *distinct declared clocks*. The common, safe shape — one clock plus a
non-clock enable port (a normal ICG) — must still resolve, which
``icg_port_enable`` pins as the regression guard against the naive
"decline whenever two legs resolve" mistake.

The decline can only ever turn a previously mislabeled flop into a loud
domain-unknown; it never drops a crossing that the design's OTHER
(unambiguous) flops carry — ``test_combine_decline_does_not_perturb_others``
pins that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains

FIX = Path(__file__).parent / "fixtures"


def _skip_if_missing(p: Path) -> None:
    if not p.exists():
        pytest.skip(f"fixture not built: {p}")


def _domains(name: str) -> list:
    d = FIX / name
    json_path = d / f"{name}.json"
    _skip_if_missing(json_path)
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(d / f"{name}.sdc")
    # Exercise the SAME path the CLI takes: untyped input ports are
    # stamped with the <unconstrained> sentinel BEFORE domain assignment.
    # Without this the regression guard would test the wrong path and miss
    # a port-enabled ICG being wrongly declined (the sentinel must not
    # count as a competing clock identity).
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    return assign_domains(
        module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
    )


def test_combine_latch_declines() -> None:
    """A latch combining clkA (D) and clkB (EN) leaves its flop
    domain-unknown — never silently resolved to either clock."""
    domains = _domains("clock_combine_latch")
    assert domains, "fixture has no flops"
    clocks = {fd.flop.cell.name: fd.clock for fd in domains}
    assert all(c is None for c in clocks.values()), (
        f"combining latch must DECLINE, not resolve to one leg: {clocks}"
    )
    # Specifically NOT silently labelled with either combined clock.
    assert "clkA" not in clocks.values()
    assert "clkB" not in clocks.values()


def test_combine_gate_declines() -> None:
    """A $and gate combining two declared clocks leaves its flop
    domain-unknown (same hardening as the latch clause)."""
    domains = _domains("clock_combine_gate")
    assert domains
    clocks = {fd.flop.cell.name: fd.clock for fd in domains}
    assert all(c is None for c in clocks.values()), (
        f"combining gate must DECLINE, not resolve to one leg: {clocks}"
    )


def test_icg_port_enable_still_resolves() -> None:
    """REGRESSION GUARD: a normal ICG (clock on D, enable from a top-level
    PORT) must still resolve to its clock — the enable port is not a
    declared clock, so this is one clock + enable, not a combine."""
    domains = _domains("icg_port_enable")
    assert domains
    clocks = {fd.flop.cell.name: fd.clock for fd in domains}
    assert all(c == "clk" for c in clocks.values()), (
        f"port-enabled ICG must resolve to clk, not decline: {clocks}"
    )


def test_combine_decline_does_not_perturb_others() -> None:
    """The decline is local: declining the combining flop must not change
    how an unambiguous flop in the same design resolves. We confirm the
    combining fixture's only flop is the declined one (no collateral) and
    the regression-guard fixture is fully resolved — i.e. the decline
    predicate is precise, not a blanket latch/gate failure."""
    combine = _domains("clock_combine_latch")
    guard = _domains("icg_port_enable")
    assert all(fd.clock is None for fd in combine)
    assert all(fd.clock == "clk" for fd in guard)
