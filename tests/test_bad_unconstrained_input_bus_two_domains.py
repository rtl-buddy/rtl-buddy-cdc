"""Bus-width multi-domain capture — CDC-011 still escalates to error.

Issue rtl-buddy-cdc#97. Same shape as the scalar two-domains fixture
but the input is ``[7:0]``. The post-pass groups crossings by
``src_port`` (not by ``(src_port, bit)``), so a fanout-of-bits doesn't
multiply the violation count — one error per port, listing every
distinct destination clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_unconstrained_input_bus_two_domains"
JSON = FIX_DIR / "bad_unconstrained_input_bus_two_domains.json"
SDC = FIX_DIR / "bad_unconstrained_input_bus_two_domains.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_port_crossings_carry_full_bus_width(context) -> None:
    """Each (port, dst flop) pair collapses across all 8 source bits;
    the resulting crossings should each have width 8 — guards against
    a regression where the port-walk emits per-bit crossings."""
    _module, crossings, _spec = context
    in_crossings = [c for c in crossings if c.src_port == "in"]
    assert len(in_crossings) == 2  # one per destination flop
    assert {c.width for c in in_crossings} == {8}


def test_cdc_011_fires_once_at_error_severity(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_011 = [v for v in violations if v.rule_id == "CDC-011"]
    assert len(cdc_011) == 1
    v = cdc_011[0]
    assert v.severity == "error"
    assert "'in'" in v.message
    assert "clk_a" in v.message
    assert "clk_b" in v.message
