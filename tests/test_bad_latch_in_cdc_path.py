"""Negative fixture: transparent latch in a CDC path (CDC-017).

Source flop in ``src_clk``, latch in between, destination flop in
``dst_clk``. The latch defeats CDC synchronisation because the
foreign-domain signal propagates transparently while the latch
enable is asserted. CDC-017 must fire; CDC-001 must stay silent
(``find_crossings`` doesn't traverse latches, so no flop-flop
crossing is detected).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_latch_in_cdc_path"
JSON = FIX_DIR / "bad_latch_in_cdc_path.json"
SDC = FIX_DIR / "bad_latch_in_cdc_path.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_017_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_017 = [v for v in violations if v.rule_id == "CDC-017"]
    assert len(cdc_017) == 1, (
        f"expected exactly one CDC-017, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    v = cdc_017[0]
    assert v.severity == "error"
    assert "transparent latch in CDC path" in v.message
    assert "src_clk" in v.message and "dst_clk" in v.message


def test_cdc_001_silent_on_latch_path(context) -> None:
    """find_crossings doesn't traverse latches, so no flop→flop
    crossing is reported. CDC-001 must therefore stay silent — the
    latch hazard is CDC-017's territory alone."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_001 = [v for v in violations if v.rule_id == "CDC-001"]
    assert cdc_001 == [], (
        f"CDC-001 leaked onto a latch-in-CDC-path shape "
        f"(would double-report with CDC-017): "
        f"{[(v.rule_id, v.message) for v in cdc_001]}"
    )
