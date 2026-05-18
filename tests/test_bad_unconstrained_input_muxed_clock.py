"""Muxed-clock destination — CDC-011 fires as a single-domain warning.

Issue rtl-buddy-cdc#97. ``d`` is untyped; the destination flop's CLK
is ``tm ? tclk : sclk``. ``trace_clock_root``'s mux walk returns the
first arm that resolves (``A`` first), so CDC-011 sees a single
resolved destination clock and emits a **warning**. The SDC author
has to assert a domain explicitly — the analyzer can't statically
know which side of the mux the chip actually runs on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_unconstrained_input_muxed_clock"
JSON = FIX_DIR / "bad_unconstrained_input_muxed_clock.json"
SDC = FIX_DIR / "bad_unconstrained_input_muxed_clock.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_tm_port_not_walked(context) -> None:
    """``tm`` only fans into the clock mux's ``S`` input — it never
    reaches a flop's ``D``. So even though it gets a sentinel entry
    (it's an untyped input), the port-walk yields no crossings for
    it. Pin: CDC-011 must not fire on ``tm``."""
    _module, crossings, _spec = context
    assert [c for c in crossings if c.src_port == "tm"] == []


def test_cdc_011_fires_as_warning_with_resolved_mux_clock(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_011 = [v for v in violations if v.rule_id == "CDC-011"]
    assert len(cdc_011) == 1
    v = cdc_011[0]
    assert v.severity == "warning"
    assert "'d'" in v.message
    # Whichever side of the mux resolved first; we just pin "exactly
    # one of the declared clocks appears" rather than which one.
    assert sum(1 for clk in ("tclk", "sclk") if clk in v.message) == 1
