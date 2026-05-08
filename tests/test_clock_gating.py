"""Positive-case fixture: integrated clock gate (ICG) is recognized.

The flop runs on a gated clock (``clk & en_latched``). The analyzer
should:
- resolve the flop's domain to ``clk`` (not ``<unresolved>``), and
- not report a CDC-008 false-positive against the AND/latch on the
  clock network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "clock_gating"
JSON = FIX_DIR / "clock_gating.json"
SDC = FIX_DIR / "clock_gating.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_flop_resolved_through_icg(context) -> None:
    """The flop's CLK pin is fed by the ICG's AND output, not directly
    by the ``clk`` port — yet trace_clock_root should still produce
    ``clk``."""
    module, _crossings, _spec = context
    domains = assign_domains(module)
    assert len(domains) == 1
    assert domains[0].clock == "clk"


def test_no_violations_on_icg(context) -> None:
    """ICG cells (and the latch driving the gate's enable) are part of
    the clock distribution network and must not be flagged by CDC-008
    even though clk reaches them."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        "unexpected violations on ICG fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
