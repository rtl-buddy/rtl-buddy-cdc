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


@pytest.mark.parametrize(
    "ctype",
    [
        "$add",  # carry chain mixes lanes
        "$sub",  # borrow chain mixes lanes
        "$mul",  # full cross-lane product
        "$shl",  # shifts remap lanes
        "$shr",
        "$sshr",
        "$div",
        "$mod",
    ],
)
def test_lane_targets_bit_mixing_cells_fall_back_to_all_outputs(ctype: str) -> None:
    """A bit-mixing cell type (not in ``_LANE_ALIGNED_TYPES``) must keep the
    conservative all-outputs fan-out: output lane i can depend on input lanes
    other than i, so restricting to ``Y[idx]`` would drop a real path. With a
    4-bit ``Y``, the lane-precise path would return a single bit ``[30]`` — so
    asserting all four outputs come back is exactly the "did NOT take the
    optimized path" check."""
    cell = _cell(ctype, A=[10, 11, 12, 13], B=[20, 21, 22, 23], Y=[30, 31, 32, 33])
    assert sorted(_lane_targets(cell, "A", 0)) == [30, 31, 32, 33]


@pytest.mark.parametrize(
    "ctype, conns",
    [
        ("$eq", dict(A=[10, 11, 12, 13], B=[20, 21, 22, 23], Y=[30])),
        ("$lt", dict(A=[10, 11, 12, 13], B=[20, 21, 22, 23], Y=[30])),
        ("$reduce_or", dict(A=[10, 11, 12, 13], Y=[30])),
        ("$reduce_and", dict(A=[10, 11, 12, 13], Y=[30])),
    ],
)
def test_lane_targets_reducing_cells_return_all_outputs(
    ctype: str, conns: dict[str, list[int]]
) -> None:
    """Reductions / comparisons collapse a bus to a narrow result — also not
    lane-aligned, so the helper returns the cell's (single-bit) output via the
    all-outputs fallback rather than indexing a non-existent lane."""
    cell = _cell(ctype, **conns)
    assert _lane_targets(cell, "A", 0) == [30]


def test_lane_targets_width_mismatch_falls_back() -> None:
    # Lane-aligned type but the port width != Y width -> all outputs (safe).
    cell = _cell("$and", A=[10, 11], B=[20, 21], Y=[30, 31, 32, 33])
    assert sorted(_lane_targets(cell, "A", 0)) == [30, 31, 32, 33]
