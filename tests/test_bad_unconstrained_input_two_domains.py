"""Multi-domain untyped-input shape — CDC-011 must escalate to error.

Issue rtl-buddy-cdc#97. The fixture's SDC omits ``set_input_delay`` for
``in``; :func:`sdc.synthesize_unconstrained_inputs` synthesises the
:data:`sdc.UNCONSTRAINED_SENTINEL` entry so the existing port-walk in
:func:`find_crossings` emits one port-sourced crossing per (port,
destination flop). ``check_cdc_011``'s post-pass groups by ``src_port``;
because ``in`` lands in two distinct clock domains (``clk_a``,
``clk_b``), the rule fires once at **error** severity rather than two
independent warnings — a single port cannot be synchronous to two
clocks, so this is intrinsically wrong regardless of the SDC opinion.

The fixture was originally landed (PR #98, commit 4eb5f49) as a
regression-baseline pinning the *pre*-CDC-011 behaviour
(``violations == []``). When CDC-011 shipped, the assertions were
flipped here; the SV / SDC / JSON were left unchanged so the same
disk-level artifact exercises both eras of behaviour.
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
    sentinel_ports = sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec, sentinel_ports


def test_sentinel_synthesised_for_unconstrained_port(context) -> None:
    """``in`` has no ``set_input_delay`` typing in the SDC →
    :func:`synthesize_unconstrained_inputs` assigns the sentinel."""
    _module, _crossings, spec, sentinel_ports = context
    assert "in" in sentinel_ports
    assert spec.port_clock["in"] == sdc_mod.UNCONSTRAINED_SENTINEL


def test_sentinel_emits_port_crossings_to_both_domains(context) -> None:
    """Port-walk emits one port-sourced crossing per (port, dst flop);
    ``in`` lands on flops in both ``clk_a`` and ``clk_b`` domains."""
    _module, crossings, _spec, _sentinel = context
    in_crossings = [c for c in crossings if c.src_port == "in"]
    assert len(in_crossings) == 2, (
        f"expected one port-sourced crossing per destination flop "
        f"(2 total); got {[(c.src_port, c.dst_clock) for c in in_crossings]}"
    )
    assert {c.dst_clock for c in in_crossings} == {"clk_a", "clk_b"}
    for c in in_crossings:
        assert c.src_clock == sdc_mod.UNCONSTRAINED_SENTINEL


def test_cdc_011_fires_once_at_error_severity(context) -> None:
    """Multi-domain post-pass consolidates the two crossings into one
    ``error``-severity violation naming both destination clocks."""
    module, crossings, spec, _sentinel = context
    violations = run_all_rules(module, crossings, spec)
    cdc_011 = [v for v in violations if v.rule_id == "CDC-011"]
    assert len(cdc_011) == 1, (
        f"expected exactly one CDC-011 violation (consolidated by the "
        f"multi-domain post-pass); got {[(v.rule_id, v.severity, v.message) for v in cdc_011]}"
    )
    v = cdc_011[0]
    assert v.severity == "error", (
        f"multi-domain capture must escalate to error; got {v.severity}"
    )
    assert "'in'" in v.message
    # Both destination clocks must appear in the message.
    assert "clk_a" in v.message
    assert "clk_b" in v.message
    # Make sure the message points the user at SDC typing as the fix.
    assert "set_input_delay" in v.message


def test_no_double_fire_on_cdc_001(context) -> None:
    """CDC-001's sentinel guard must prevent it from firing on the
    sentinel-sourced crossing (CDC-011 owns this shape, with different
    fix advice)."""
    module, crossings, spec, _sentinel = context
    violations = run_all_rules(module, crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert cdc_001 == [], (
        f"CDC-001 must skip sentinel-sourced crossings — they're "
        f"CDC-011's territory. Got {[(v.rule_id, v.message) for v in cdc_001]}"
    )
