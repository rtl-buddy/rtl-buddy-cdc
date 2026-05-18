"""Regression-baseline test for the untyped-input multi-domain gap.

Issue: rtl-buddy-cdc#97 — when a top-level input port has no
``set_input_delay -clock`` typing, ``find_crossings`` never iterates
it (the port-walk at ``domain.py:413-481`` is keyed off
``ClockSpec.port_clock``), so a crossing that physically lands the
port in two distinct clock domains reports clean. This test pins that
current behaviour so any later change (CDC-011 landing, or a
data-model change that retroactively walks untyped ports) is forced
to update the assertions here.

When CDC-011 lands, flip the assertions to expect one error-severity
violation naming ``in`` and both ``{clk_a, clk_b}`` destinations — a
single port cannot be synchronous to two distinct clocks, so it is
intrinsically wrong regardless of SDC opinion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_unconstrained_input_two_domains"
JSON = FIX_DIR / "bad_unconstrained_input_two_domains.json"
SDC = FIX_DIR / "bad_unconstrained_input_two_domains.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_sdc_has_no_input_delay(context) -> None:
    """Sanity: confirm the SDC really omits ``set_input_delay`` for
    ``in``. If a future SDC edit adds typing, this fixture stops
    documenting the gap and should be re-thought."""
    _module, _crossings, spec = context
    assert "in" not in spec.port_clock, (
        "Fixture invariant violated: `in` got a port_clock entry. "
        "The SDC must leave `in` untyped to exercise the #97 gap."
    )


def test_untyped_port_emits_no_crossings(context) -> None:
    """Today: ``in`` has no ``port_clock`` entry → no port-sourced
    Crossing is emitted, even though it physically lands on flops in
    two different clock domains. When CDC-011 lands, flip this to
    assert >=1 port-sourced crossing whose ``src_port == 'in'``."""
    _module, crossings, _spec = context
    port_crossings = [c for c in crossings if c.src_port == "in"]
    assert port_crossings == [], (
        f"Untyped port `in` was walked unexpectedly: got "
        f"{[(c.src_port, c.dst_clock) for c in port_crossings]}. "
        "CDC-011 (#97) may now be implemented — flip this assertion "
        "to expect crossings landing in both clk_a and clk_b."
    )


def test_rule_pack_reports_clean(context) -> None:
    """Today: with no port-sourced crossings, the rule pack stays
    silent on the untyped-port-to-two-domains shape. When CDC-011
    lands, flip to expect one error-severity violation naming ``in``
    and both destination clocks ``{clk_a, clk_b}``."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        f"Expected zero violations today; got "
        f"{[(v.rule_id, v.severity, v.message) for v in violations]}. "
        "CDC-011 (#97) may now fire — update the assertion to expect "
        "one error-severity violation naming `in` and both clk_a/clk_b."
    )
