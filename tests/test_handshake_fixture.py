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

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.rules import check_cdc_002, run_all as run_all_rules

FIXTURE = (
    Path(__file__).parent / "fixtures" / "ip_cdc_handshake" / "ip_cdc_handshake.json"
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


def test_golden_passes_all_rules(module) -> None:
    """The golden ip_cdc_handshake design uses 2FF synchronizers on both
    control crossings (req sync, ack sync), so CDC-001 should not fire.
    The 8-bit data crossing is intentionally skipped by CDC-001 — it's
    a bus crossing, governed by a future CDC-004 rule."""
    sdc_path = (
        Path(__file__).parent / "fixtures" / "ip_cdc_handshake" / "ip_cdc_handshake.sdc"
    )
    spec = sdc_mod.parse_file(sdc_path)
    assert spec.are_async("src_clk", "dst_clk")

    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    # SDC marks src_clk and dst_clk as asynchronous, so every detected
    # crossing should be in scope for rule checks.
    assert len(async_crossings) == 3

    violations = run_all_rules(module, async_crossings)
    assert violations == [], (
        f"unexpected violations on golden fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_cdc_002_fires_when_threshold_raised(module) -> None:
    """The two control crossings have 2-deep synchronizers — silent at
    the default required_depth=2 (CDC-001 territory only) but should
    fire CDC-002 once a project requires 3-deep chains."""
    sdc_path = (
        Path(__file__).parent / "fixtures" / "ip_cdc_handshake" / "ip_cdc_handshake.sdc"
    )
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]

    raised = check_cdc_002(module, async_crossings, required_depth=3)
    # Two single-bit control crossings (req sync, ack sync) both have
    # depth=2 → should each report once when required_depth is 3.
    assert len(raised) == 2
    assert all(v.rule_id == "CDC-002" for v in raised)
    assert all(v.severity == "warning" for v in raised)
