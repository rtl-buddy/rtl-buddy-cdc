"""CDC-012 — feedback presence is a crossing-level property.

Regression guard for rtl-buddy-cdc#239. Two independent multi-bit
gated-bus crossings share one src_clk -> dst_clk async pair:

  * Channel A is a proper req/ack handshake (synced-back ack) — CDC-012
    must stay silent on it.
  * Channel B is the broken req-only form — CDC-012 must fire on it.

The bug: a feedback cache keyed on the (src_clk, dst_clk) domain pair
saw channel A's ack feedback and short-circuited *every* gated crossing
in that pair, silencing the broken channel B. The fix scopes the
feedback check to each crossing's own source flop, so B still fires
while A stays silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_mixed_handshake_datahold"
JSON = FIX_DIR / "bad_mixed_handshake_datahold.json"
SDC = FIX_DIR / "bad_mixed_handshake_datahold.sdc"


def _q_driven_netname(module: netlist.Module, cell_name: str) -> str | None:
    """The netname driven by ``cell_name``'s ``Q`` output, if any.

    Yosys keeps the RTL signal name on the netname, not the flop cell,
    so this maps the violation's structural ``$procdff$NN`` back to a
    human-readable name (``a_payload`` / ``b_payload``).
    """
    q_bits = module.cells[cell_name].connections.get("Q")
    if not q_bits:
        return None
    for name, net in module.netnames.items():
        if net.bits == q_bits:
            return name
    return None


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def test_cdc_012_fires_only_on_the_broken_channel(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    cdc_012 = [v for v in violations if v.rule_id == "CDC-012"]
    assert len(cdc_012) == 1, (
        "expected exactly one CDC-012 (broken channel B only), got "
        f"{[(v.rule_id, v.cell_name) for v in violations]}"
    )
    v = cdc_012[0]
    assert v.severity == "warning"
    assert v.crossing is not None and v.crossing.width == 8
    assert "src_clk" in v.message and "dst_clk" in v.message
    assert "req/ack handshake" in v.message  # fix advice

    # The fire must land on channel B (the broken req-only crossing),
    # never channel A (the proper handshake). Before #239 the per-domain
    # cache silenced B because of A's ack feedback.
    src_name = _q_driven_netname(module, v.crossing.src_flop.cell.name)
    dst_name = _q_driven_netname(module, v.crossing.dst_flop.cell.name)
    assert src_name == "b_payload", f"CDC-012 fired on {src_name!r}, not b_payload"
    assert dst_name == "b_dst_data", f"CDC-012 dst is {dst_name!r}, not b_dst_data"


def test_only_cdc_012_fires(context) -> None:
    """Channel A is a textbook handshake (zero violations) and channel
    B's broken crossing produces exactly one CDC-012 — nothing else."""
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    rule_ids = {v.rule_id for v in violations}
    assert rule_ids == {"CDC-012"}, f"unexpected rules fired: {rule_ids}"
