"""Lane-aware data fanout in ``find_crossings`` (issue #258).

``_lane_targets`` keeps the BFS bit-precise for width-preserving bitwise / mux
cells (input lane ``idx`` -> output ``Y[idx]``) and falls back to all-outputs
for bit-mixing cells. This removes the O(width^2) all-outputs fan-out on wide
buses *without* changing the set of reported crossings.

Two fixtures pin the end-to-end equivalence — a lane-aligned ``$and`` bus and a
lane-mixing ``$add`` bus must both still report the single width-16 src->dst
crossing — and unit cases pin the ``_lane_targets`` contract directly (the
lane-precision isn't observable in the per-(src,dst) ``Crossing`` output, which
stays width-16 either way).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.domain import _lane_targets, find_crossings
from rtl_buddy_cdc.netlist import Cell

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "name",
    ["wide_bus_lane_crossing", "wide_bus_mixing_crossing"],
)
def test_wide_bus_crossing_detected(name: str) -> None:
    """Both the lane-aligned ($and) and lane-mixing ($add) wide buses must
    report exactly one src->dst crossing at the full 16-bit width — the mixing
    case is the guard that the lane-aware fanout never drops a cross-lane path."""
    json_path = FIX / name / f"{name}.json"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    crossings = find_crossings(module)
    assert len(crossings) == 1
    c = crossings[0]
    assert (c.src_clock, c.dst_clock) == ("src_clk", "dst_clk")
    assert c.width == 16


def _cell(ctype: str, **conns: list[int]) -> Cell:
    return Cell(
        name="u", type=ctype, connections={k: tuple(v) for k, v in conns.items()}
    )


def test_lane_targets_bitwise_is_bit_precise() -> None:
    cell = _cell("$and", A=[10, 11, 12, 13], B=[20, 21, 22, 23], Y=[30, 31, 32, 33])
    assert _lane_targets(cell, "A", 2) == [32]
    assert _lane_targets(cell, "B", 0) == [30]


def test_lane_targets_mux_data_lane_precise_select_all_outputs() -> None:
    # $mux data ports A/B are lane-aligned; the 1-bit select drives every lane.
    cell = _cell(
        "$mux", A=[10, 11, 12, 13], B=[20, 21, 22, 23], S=[40], Y=[30, 31, 32, 33]
    )
    assert _lane_targets(cell, "A", 1) == [31]
    assert sorted(_lane_targets(cell, "S", 0)) == [30, 31, 32, 33]


def test_lane_targets_mixing_cell_falls_back_to_all_outputs() -> None:
    # $add carry chain mixes lanes -> must keep the conservative all-outputs walk.
    cell = _cell("$add", A=[10, 11, 12, 13], B=[20, 21, 22, 23], Y=[30, 31, 32, 33])
    assert sorted(_lane_targets(cell, "A", 0)) == [30, 31, 32, 33]


def test_lane_targets_width_mismatch_falls_back() -> None:
    # Lane-aligned type but the port width != Y width -> all outputs (safe).
    cell = _cell("$and", A=[10, 11], B=[20, 21], Y=[30, 31, 32, 33])
    assert sorted(_lane_targets(cell, "A", 0)) == [30, 31, 32, 33]
