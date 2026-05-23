"""CDC-016 — opposite-edge synchroniser (issue #175).

The fixture is a 2FF chain on ``dst_clk`` whose stages disagree on
clock-pin polarity (stage 1 ``posedge``, stage 2 ``negedge``). The
RTL is syntactically symmetric — CDC-001 / CDC-002 see a valid 2-flop
chain and stay silent. CDC-016 walks the chain via
``_sync_chain_flops`` and fires when adjacent stages disagree on the
Yosys ``CLK_POLARITY`` parameter.

Each metastable value has only half a clock period to resolve
instead of a full period, roughly halving MTBF. The failure is
silent: simulation doesn't see it, the depth check doesn't see it,
the chain is structurally a valid two-flop synchroniser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_opposite_edge_sync"
JSON = FIX_DIR / "bad_opposite_edge_sync.json"
SDC = FIX_DIR / "bad_opposite_edge_sync.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_016_fires_once(context) -> None:
    """One finding per offending chain (not per adjacent-pair)."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    cdc_016 = [v for v in violations if v.rule_id == "CDC-016"]
    assert len(cdc_016) == 1, (
        f"expected exactly one CDC-016 finding, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    assert "posedge" in cdc_016[0].message
    assert "negedge" in cdc_016[0].message


def test_only_cdc_016_fires(context) -> None:
    """Structural depth check sees a valid 2FF chain — CDC-001 /
    CDC-002 must stay silent. The polarity hazard is CDC-016's alone."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    assert rule_ids == ["CDC-016"], (
        f"expected CDC-016 only, got {[(v.rule_id, v.message) for v in violations]}"
    )


def test_good_2ff_sync_does_not_regress() -> None:
    """The existing same-edge 2FF fixture must not fire CDC-016 —
    sanity check that the polarity walk doesn't false-positive on a
    clean chain."""
    good_dir = Path(__file__).parent / "fixtures" / "good_2ff_sync"
    json_path = good_dir / "good_2ff_sync.json"
    sdc_path = good_dir / "good_2ff_sync.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    violations = run_all_rules(module, crossings, spec)
    cdc_016 = [v for v in violations if v.rule_id == "CDC-016"]
    assert cdc_016 == [], f"CDC-016 false-positive on good_2ff_sync: {cdc_016}"
