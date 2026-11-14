"""Branch-level unit tests for the single-bit handshake acceptance
(rtl-buddy-cdc#368): ``_carries_synchronised_control`` and the
``require_hold`` form of ``_gated_capture_fanin``. The fixture-level
verdicts are in ``test_single_bit_handshake_capture.py``.

Synthetic netlist: ``clk_s`` / ``clk_d`` are clock ports (bits 1 / 2),
so each flop's domain is its clock port's name. ``t`` is the source
register (``Q`` = 10); ``h`` → ``x`` → ``y`` is a straight line of
``clk_d`` flops behind it (``Q`` = 11, 12, 13).
"""

from __future__ import annotations

from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.rules import (
    _build_context,
    _carries_synchronised_control,
    _gated_capture_fanin,
    _single_bit_handshake_capture,
)

CLK_S: Bit = 1
CLK_D: Bit = 2


def _dff(name: str, d: tuple[Bit, ...], q: tuple[Bit, ...], clk: Bit = CLK_D) -> Cell:
    return Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})


def _module(*cells: Cell) -> Module:
    ports = {
        "clk_s": Port(name="clk_s", direction="input", bits=(CLK_S,)),
        "clk_d": Port(name="clk_d", direction="input", bits=(CLK_D,)),
        "din": Port(name="din", direction="input", bits=(50,)),
        "rst": Port(name="rst", direction="input", bits=(60,)),
        "out": Port(name="out", direction="output", bits=(13,)),
    }
    return Module(name="m", ports=ports, cells={c.name: c for c in cells}, netnames={})


def _line(h_d: tuple[Bit, ...] = (10,), *extra: Cell) -> Module:
    return _module(
        _dff("t", (50,), (10,), clk=CLK_S),
        _dff("h", h_d, (11,)),
        _dff("x", (11,), (12,)),
        _dff("y", (12,), (13,)),
        *extra,
    )


def _carries(module: Module, name: str) -> bool:
    ctx = _build_context(module, None)
    flop = next(f for f in ctx.flops if f.cell.name == name)
    return _carries_synchronised_control(module, flop, "clk_s", "clk_d", ctx)


def test_stages_past_the_first_and_their_delays_carry_the_control() -> None:
    module = _line()
    assert _carries(module, "x")
    assert _carries(module, "y")
    assert not _carries(module, "h")  # first stage: may be metastable
    assert not _carries(module, "t")  # not in the destination domain


def test_first_stage_read_twice_is_no_synchroniser() -> None:
    tap = Cell(name="tap", type="$not", connections={"A": (11,), "Y": (40,)})
    assert not _carries(_line((10,), tap), "x")


def test_head_behind_its_own_local_reset_mux() -> None:
    rmux = Cell(
        name="rmux",
        type="$mux",
        connections={"A": (10,), "B": ("0",), "S": (60,), "Y": (20,)},
    )
    assert _carries(_line((20,), rmux), "x")


def test_logic_in_the_line_breaks_it() -> None:
    inv = Cell(name="inv", type="$not", connections={"A": (10,), "Y": (21,)})
    assert not _carries(_line((21,), inv), "x")


def test_line_from_a_multi_bit_flop_is_rejected() -> None:
    wide = _dff("w", (50, 51), (30, 31))
    module = _module(
        _dff("t", (50,), (10,), clk=CLK_S),
        wide,
        _dff("x", (30,), (12,)),
        _dff("y", (12,), (13,)),
    )
    assert not _carries(module, "y")


def test_self_loop_never_reaches_a_head() -> None:
    module = _module(_dff("t", (50,), (10,), clk=CLK_S), _dff("z", (14,), (14,)))
    assert not _carries(module, "z")


def _capture(d_driver: Cell, d_bit: Bit, *extra: Cell) -> tuple[Module, Crossing]:
    """``t`` → ``cap`` with ``cap.D`` driven by ``d_driver``."""
    cap = _dff("cap", (d_bit,), (70,))
    module = _module(
        _dff("t", (50,), (10,), clk=CLK_S),
        _dff("s", (12,), (13,)),
        cap,
        d_driver,
        *extra,
    )
    c = Crossing(
        src_clock="clk_s",
        dst_clock="clk_d",
        dst_flop=Flop(cell=cap, clk=CLK_D, d=(d_bit,), q=(70,)),
        width=1,
        min_hops=1,
        src_flop=Flop(cell=module.cells["t"], clk=CLK_S, d=(50,), q=(10,)),
    )
    return module, c


def test_hold_requires_a_mux_on_d() -> None:
    gate = Cell(name="g", type="$and", connections={"A": (10,), "B": (13,), "Y": (71,)})
    module, c = _capture(gate, 71)
    ctx = _build_context(module, None)
    assert _gated_capture_fanin(module, c, ctx.domains, ctx.bit_drivers) is None
    assert (
        _gated_capture_fanin(module, c, ctx.domains, ctx.bit_drivers, require_hold=True)
        is None
    )


def test_hold_mux_without_cached_reader_counts() -> None:
    hold = Cell(
        name="hold",
        type="$mux",
        connections={"A": (70,), "B": (10,), "S": (13,), "Y": (71,)},
    )
    module, c = _capture(hold, 71)
    ctx = _build_context(module, None)
    assert _gated_capture_fanin(
        module, c, ctx.domains, ctx.bit_drivers, require_hold=True
    ) == {"s"}


def test_bus_crossings_are_not_single_bit_captures() -> None:
    hold = Cell(
        name="hold",
        type="$mux",
        connections={"A": (70,), "B": (10,), "S": (13,), "Y": (71,)},
    )
    module, c = _capture(hold, 71)
    ctx = _build_context(module, None)
    wide = Crossing(
        src_clock=c.src_clock,
        dst_clock=c.dst_clock,
        dst_flop=c.dst_flop,
        width=2,
        min_hops=1,
        src_flop=c.src_flop,
    )
    assert _single_bit_handshake_capture(module, wide, ctx) is None
