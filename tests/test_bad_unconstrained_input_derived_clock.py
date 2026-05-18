"""Derived-clock destination — CDC-011 fires as a single-domain warning.

Issue rtl-buddy-cdc#97. ``in`` is untyped; the destination flop's CLK
is ``clk_a & clk_b & clk_c`` (a two-level ``$and`` tree). The clock-
trace's ``a_root or b_root`` short-circuit picks one of the three
inputs as the resolved root, so CDC-011's grouping sees a single
destination clock for ``in`` and emits a **warning**, not the multi-
domain ``error`` shape (that's covered by
``test_bad_unconstrained_input_two_domains``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_unconstrained_input_derived_clock"
JSON = FIX_DIR / "bad_unconstrained_input_derived_clock.json"
SDC = FIX_DIR / "bad_unconstrained_input_derived_clock.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_011_fires_as_warning_with_resolved_dst_clock(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_011 = [v for v in violations if v.rule_id == "CDC-011"]
    assert len(cdc_011) == 1
    v = cdc_011[0]
    assert v.severity == "warning", (
        f"single resolved destination → warning; got {v.severity}"
    )
    assert "'in'" in v.message
    # The message must include exactly one of the three declared
    # clocks (whichever trace_clock_root resolved to). We don't pin
    # which one because the $and-tree short-circuit order is a
    # trace-implementation detail; what matters is the rule decided
    # on a single domain rather than reporting "multiple".
    assert sum(1 for clk in ("clk_a", "clk_b", "clk_c") if clk in v.message) == 1, (
        f"warning message should name a single resolved destination "
        f"clock from the AND tree; got: {v.message}"
    )
