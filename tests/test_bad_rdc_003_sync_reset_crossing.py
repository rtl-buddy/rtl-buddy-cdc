"""Negative-case fixture for RDC-003 — sync reset crossing.

Issue #114 (RDC-002 through RDC-005 rule pack), second sub-PR.

The fixture wires a source-domain flop's ``Q`` directly into a
destination-domain flop's ``SRST`` (synchronous reset) pin, no 2FF
sync chain. The sync reset is sampled on the destination clock and
the cross-domain source can be metastable on the sample cycle.

RDC-003 must fire once on the consumer flop. The sync-reset pin is
also a capture input of the crossing walk (rtl-buddy-cdc#375), so the
unsynchronised ``src_rst`` → ``q_dst`` crossing reports CDC-001, as it
does on the ``proc`` form of the same RTL (reset mux on ``D``). No other
rule fires (no comb-on-D, no polarity mismatch — RDC-002 explicitly
skips sync-reset consumers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_rdc_003_sync_reset_crossing"
JSON = FIX_DIR / "bad_rdc_003_sync_reset_crossing.json"
SDC = FIX_DIR / "bad_rdc_003_sync_reset_crossing.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
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
    return module, async_crossings, spec


def test_rdc_003_fires_once(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rdc_003 = [v for v in violations if v.rule_id == "RDC-003"]
    assert len(rdc_003) == 1, (
        f"expected exactly one RDC-003, got {[v.rule_id for v in violations]}"
    )
    v = rdc_003[0]
    assert v.severity == "error"
    assert "sync reset crossing" in v.message
    assert "SRST" in v.message
    assert "src_clk" in v.message and "dst_clk" in v.message
    assert "2FF reset synchroniser" in v.message
    # The anchor is the consumer flop (the one with the SRST pin).
    assert v.cell_name is not None
    assert "ff.cc" in v.cell_name or v.cell_name.startswith("$"), (
        f"expected an SRST-bearing flop cell name, got {v.cell_name!r}"
    )


def test_no_other_rules_fire(context) -> None:
    """RDC-002 explicitly skips sync-reset consumers, so even though
    the producer's ARST_VALUE differs from the consumer's
    SRST_POLARITY, RDC-002 stays silent. Besides RDC-003, only the
    crossing's own CDC-001 fires (once)."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = sorted(v.rule_id for v in violations)
    assert rule_ids == ["CDC-001", "RDC-003"], f"unexpected rules fired: {rule_ids}"


def test_sync_reset_pin_is_a_crossing(context) -> None:
    """``src_rst`` reaches ``q_dst`` only on its ``SRST`` pin; the walk
    counts the folded reset mux as the one cell on the path."""
    _module, async_crossings, _spec = context
    (c,) = async_crossings
    assert (c.src_clock, c.dst_clock, c.min_hops, c.width) == (
        "src_clk",
        "dst_clk",
        1,
        1,
    )
