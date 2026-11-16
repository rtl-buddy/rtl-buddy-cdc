"""CDC-020 counts slices, not full-width captures (rtl-buddy-cdc#386, item 2).

CDC-020 is the sliced-bus rule: a multi-bit source register whose bits
cross to the same destination domain through several *narrower*
crossings, each resolving on its own schedule. It used to count every
destination of the source, whatever the crossing's width, so two
full-width captures of one held payload under one synchronised enable —
two properly gated buses, accepted by CDC-004 — read as a sliced bus.
A crossing that lands the whole bus on one destination is a bus crossing
(CDC-004's to judge) and is no longer a lane; slices still are, alongside
it or not. The fixture ``good_gated_bus_double_capture`` is the
end-to-end case (``tests/test_good_fixtures.py``).
"""

from __future__ import annotations

from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import check_cdc_020

SCLK, DCLK = 2, 3
SRC_Q = (4, 5, 6, 7)


def _module(dsts: dict[str, tuple[int, ...]]) -> Module:
    """A 4-bit source register and one destination register per entry,
    ``name -> the source bits it captures``."""
    cells = {
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (SCLK,), "D": (10, 11, 12, 13), "Q": SRC_Q},
        )
    }
    q = 100
    for name, d in dsts.items():
        cells[name] = Cell(
            name=name,
            type="$dff",
            connections={
                "CLK": (DCLK,),
                "D": d,
                "Q": tuple(range(q, q + len(d))),
            },
        )
        q += len(d)
    return Module(
        name="m",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(SCLK,)),
            "dclk": Port(name="dclk", direction="input", bits=(DCLK,)),
        },
        cells=cells,
        netnames={},
    )


def _crossings(module: Module) -> list[Crossing]:
    flops: dict[str, Flop] = {f.cell.name: f for f in find_flops(module)}
    return [
        Crossing(
            src_clock="sclk",
            dst_flop=f,
            dst_clock="dclk",
            min_hops=0,
            width=len(f.d),
            src_flop=flops["src"],
        )
        for name, f in flops.items()
        if name != "src"
    ]


def test_two_full_width_captures_are_not_a_sliced_bus() -> None:
    module = _module({"cap_a": SRC_Q, "cap_b": SRC_Q})
    assert check_cdc_020(module, _crossings(module)) == []


def test_one_bit_lanes_still_fire() -> None:
    module = _module({f"lane{i}": (b,) for i, b in enumerate(SRC_Q)})
    (v,) = check_cdc_020(module, _crossings(module))
    assert v.rule_id == "CDC-020" and "4 per-lane crossings" in v.message


def test_slices_beside_a_full_width_capture_still_fire() -> None:
    module = _module({"cap": SRC_Q, "lo": SRC_Q[:2], "hi": SRC_Q[2:]})
    (v,) = check_cdc_020(module, _crossings(module))
    assert "2 per-lane crossings" in v.message
    assert "cap," not in v.message and "(hi, lo)" in v.message


def test_one_slice_beside_a_full_width_capture_is_not_sliced() -> None:
    module = _module({"cap": SRC_Q, "lo": SRC_Q[:2]})
    assert check_cdc_020(module, _crossings(module)) == []
