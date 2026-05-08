"""Analyzer behaviour against the ip_cdc_handshake golden fixture.

The fixture's flattened netlist JSON is committed alongside the SV
sources so the tests don't require a yosys install. Regenerate with::

    PATH=/path/to/yosys:$PATH yosys -p "
      read_verilog -sv tests/fixtures/ip_cdc_handshake/ip_cdc_sync.sv \
                       tests/fixtures/ip_cdc_handshake/ip_cdc_handshake.sv;
      hierarchy -top ip_cdc_handshake;
      proc; flatten; opt_clean;
      write_json tests/fixtures/ip_cdc_handshake/ip_cdc_handshake.json"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.domain import assign_domains, find_crossings

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "ip_cdc_handshake"
    / "ip_cdc_handshake.json"
)


@pytest.fixture(scope="module")
def module():
    if not FIXTURE.exists():
        pytest.skip(f"fixture not built: {FIXTURE}")
    return netlist.load(FIXTURE)


def test_top_module_loaded(module) -> None:
    assert module.name == "ip_cdc_handshake"
    assert {"src_clk", "dst_clk", "src_data", "dst_data"} <= set(module.ports)


def test_flop_domains(module) -> None:
    by_clock: dict[str | None, int] = {}
    for fd in assign_domains(module):
        by_clock[fd.clock] = by_clock.get(fd.clock, 0) + 1
    # 4 src-side flops (1× src_req, 8-bit src_payload counted as 1 cell, ...)
    # 6 dst-side flops (req_d1, ack, valid, dst_data, plus the two 2FF sync chains)
    # Yosys collapses multi-bit FFs into a single cell, so these counts are
    # cell-counts, not bit-counts.
    assert by_clock.get(None, 0) == 0, "every flop should resolve to a clock port"
    assert by_clock.get("src_clk", 0) == 4
    assert by_clock.get("dst_clk", 0) == 6


def test_crossings(module) -> None:
    crossings = find_crossings(module)
    # Three distinct flop→flop crossings: the two control synchronizers
    # (each width=1, hops=0 since they go straight into the sync chain
    # first stage) and the 8-bit gated data path (width=8, hops≥1 due
    # to the dst-side mux).
    assert len(crossings) == 3

    src_to_dst = [c for c in crossings if c.src_clock == "src_clk"]
    dst_to_src = [c for c in crossings if c.src_clock == "dst_clk"]
    assert len(src_to_dst) == 2  # req sync + data path
    assert len(dst_to_src) == 1  # ack sync

    # The single dst→src crossing is the ack synchronizer first stage:
    # direct flop-to-flop, 1 bit.
    ack_sync = dst_to_src[0]
    assert ack_sync.width == 1
    assert ack_sync.min_hops == 0

    # The data crossing is the only multi-bit one.
    data = next(c for c in src_to_dst if c.width > 1)
    assert data.width == 8
    assert data.min_hops >= 1
