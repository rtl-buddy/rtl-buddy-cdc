"""Who reads a net bit, and the smaller rtl-buddy-cdc#344 findings.

``rules._bit_reader_count`` is the single definition of "who reads this
bit" behind every single-reader test in the rule pack. A module output
port is a reader (the value is used outside the module), and a cell's
output pin is not (it drives the bit), whether the cell is a Yosys
primitive (``Y`` / ``Q``) or a blackbox instance whose ``port_directions``
name the outputs.

Also here: CDC-008 must not report a blackbox's forwarded clock output
pin as clock-as-data, and CDC-019 must see a decoder that Yosys lowered
to an asynchronous ROM read.
"""

from __future__ import annotations

import json
from pathlib import Path

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import (
    _async_rom_read_drivers,
    _bit_reader_count,
    _build_context,
    check_cdc_008,
    check_cdc_019,
)

FIX = Path(__file__).parent / "fixtures"


def _ff(name: str, clk: int, d: tuple[int, ...], q: tuple[int, ...]) -> Cell:
    return Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})


# --- _bit_reader_count -------------------------------------------------------


def test_output_and_inout_ports_are_readers_input_ports_are_not() -> None:
    cells = {"f": _ff("f", 2, (3,), (4,))}
    ports = {
        "clk": Port(name="clk", direction="input", bits=(2,)),
        "d": Port(name="d", direction="input", bits=(3,)),
        "q": Port(name="q", direction="output", bits=(4,)),
        "io": Port(name="io", direction="inout", bits=(4,)),
    }
    counts = _bit_reader_count(Module("m", ports, cells, {}))
    assert counts[3] == 1  # flop D only; the input port drives it
    assert counts[4] == 2  # output port + inout port
    assert 2 not in counts  # CLK is not a data reader


def test_cell_output_pins_follow_port_directions() -> None:
    """A blackbox instance's output pins drive their nets; only its input
    pins read. Without ``port_directions`` only ``Y`` / ``Q`` count as
    outputs, so the legacy hand-built shape keeps its old count."""
    bb = Cell(
        name="u_ip",
        type="ip_core",
        connections={"clk": (2,), "din": (5,), "clk_out": (6,), "dout": (7,)},
        port_directions={
            "clk": "input",
            "din": "input",
            "clk_out": "output",
            "dout": "output",
        },
    )
    assert bb.is_output_pin("clk_out")
    assert bb.is_output_pin("dout")
    assert not bb.is_output_pin("din")
    counts = _bit_reader_count(Module("m", {}, {"u_ip": bb}, {}))
    assert counts == {2: 1, 5: 1}

    legacy = Cell(name="g", type="$and", connections={"A": (5,), "B": (5,), "Y": (6,)})
    assert legacy.is_output_pin("Y")
    assert not legacy.is_output_pin("A")
    assert _bit_reader_count(Module("m", {}, {"g": legacy}, {})) == {5: 2}


def test_inout_cell_pin_is_a_reader() -> None:
    cell = Cell(
        name="pad",
        type="iobuf",
        connections={"IO": (5,)},
        port_directions={"IO": "inout"},
    )
    assert not cell.is_output_pin("IO")
    assert _bit_reader_count(Module("m", {}, {"pad": cell}, {})) == {5: 1}


def test_context_reader_counts_match_the_helper() -> None:
    """``_RuleContext.reader_counts`` used to be a second, inline copy of
    the count; it is now the helper's result, ports included."""
    cells = {"s0": _ff("s0", 2, (3,), (4,)), "s1": _ff("s1", 2, (4,), (5,))}
    ports = {
        "clk": Port(name="clk", direction="input", bits=(2,)),
        "tap": Port(name="tap", direction="output", bits=(4,)),
    }
    module = Module("m", ports, cells, {})
    ctx = _build_context(module, None)
    assert ctx.reader_counts == _bit_reader_count(module)
    assert ctx.reader_counts[4] == 2


def test_loader_reads_port_directions() -> None:
    path = FIX / "clock_output_blackbox" / "clock_output_blackbox.json"
    top, _bbs = netlist.load_with_blackboxes(path)
    u0 = top.cells["u0"]
    assert u0.port_directions["clk_out"] == "output"
    assert u0.is_output_pin("clk_out")
    assert not u0.is_output_pin("clk_in")
    raw = json.loads(path.read_text())["modules"]["top"]["cells"]["u0"]
    assert u0.port_directions == raw["port_directions"]


# --- CDC-008: blackbox clock output ------------------------------------------


def _clock_output_module(*, data_pin_gets_clock: bool = False) -> Module:
    """``clk_a`` (bit 2) enters blackbox ``u_ip`` on ``clk``; its
    ``clk_out`` (bit 6) clocks flop ``q0`` in the parent. Optionally
    ``clk_a`` is also wired into the blackbox's data input ``din``."""
    bb = Cell(
        name="u_ip",
        type="ip_core",
        connections={
            "clk": (2,),
            "din": (2,) if data_pin_gets_clock else (4,),
            "clk_out": (6,),
            "dout": (7,),
        },
        port_directions={
            "clk": "input",
            "din": "input",
            "clk_out": "output",
            "dout": "output",
        },
    )
    cells = {
        "s0": _ff("s0", 2, (3,), (4,)),
        "u_ip": bb,
        "q0": _ff("q0", 6, (7,), (8,)),
    }
    ports = {
        "clk_a": Port(name="clk_a", direction="input", bits=(2,)),
        "d_in": Port(name="d_in", direction="input", bits=(3,)),
        "q_out": Port(name="q_out", direction="output", bits=(8,)),
    }
    return Module("top", ports, cells, {})


def test_cdc_008_ignores_a_blackbox_clock_output_pin() -> None:
    """The forwarded clock's own source pin is not a use of it as data."""
    module = _clock_output_module()
    ctx = _build_context(
        module,
        None,
        blackbox_modules=frozenset({"ip_core"}),
        boundary_clock_pins={"u_ip": frozenset({"clk"})},
    )
    assert check_cdc_008(module, [], None, ctx=ctx) == []


def test_cdc_008_still_fires_on_a_blackbox_data_input() -> None:
    module = _clock_output_module(data_pin_gets_clock=True)
    ctx = _build_context(
        module,
        None,
        blackbox_modules=frozenset({"ip_core"}),
        boundary_clock_pins={"u_ip": frozenset({"clk"})},
    )
    violations = check_cdc_008(module, [], None, ctx=ctx)
    assert len(violations) == 1
    assert "u_ip.din[0]" in violations[0].message


# --- CDC-019: decoder lowered to a ROM ---------------------------------------


def _rom_decoder_module(*, clk_enable: str = "0") -> Module:
    """A 2-bit decode table read from an async ``$memrd_v2`` (``DATA``
    bits 10, 11) registered by two src flops, each crossing into its own
    2FF synchroniser on ``dst_clk``.

    Bits: 2 = src_clk, 3 = dst_clk, 4 = addr, 10/11 = ROM DATA,
    20/21 = src flop Q, 30/31 = first stage, 40/41 = second stage.
    """
    rom = Cell(
        name="rom",
        type="$memrd_v2",
        connections={
            "ADDR": (4,),
            "CLK": ("x",) if clk_enable == "0" else (2,),
            "DATA": (10, 11),
            "EN": ("1",),
            "ARST": ("0",),
            "SRST": ("0",),
        },
        parameters={"CLK_ENABLE": clk_enable, "WIDTH": "10"},
        port_directions={
            "ADDR": "input",
            "CLK": "input",
            "DATA": "output",
            "EN": "input",
            "ARST": "input",
            "SRST": "input",
        },
    )
    cells: dict[str, Cell] = {"rom": rom}
    for lane in (0, 1):
        cells[f"l{lane}"] = _ff(f"l{lane}", 2, (10 + lane,), (20 + lane,))
        cells[f"m{lane}"] = _ff(f"m{lane}", 3, (20 + lane,), (30 + lane,))
        cells[f"s{lane}"] = _ff(f"s{lane}", 3, (30 + lane,), (40 + lane,))
    ports = {
        "src_clk": Port(name="src_clk", direction="input", bits=(2,)),
        "dst_clk": Port(name="dst_clk", direction="input", bits=(3,)),
        "sel": Port(name="sel", direction="input", bits=(4,)),
        "out": Port(name="out", direction="output", bits=(40, 41)),
    }
    return Module("rom_dec", ports, cells, {})


def test_async_rom_read_drivers() -> None:
    assert _async_rom_read_drivers(_rom_decoder_module()) == {10: "rom", 11: "rom"}
    # A clocked read port is a register stage, not a decoder.
    assert _async_rom_read_drivers(_rom_decoder_module(clk_enable="1")) == {}


def test_cdc_019_fires_on_a_rom_lowered_decoder() -> None:
    module = _rom_decoder_module()
    crossings = find_crossings(module)
    assert len(crossings) == 2
    violations = check_cdc_019(module, crossings)
    assert [v.rule_id for v in violations] == ["CDC-019"]
    assert "rom drives 2 separate" in violations[0].message


def test_cdc_019_ignores_a_clocked_rom_read() -> None:
    module = _rom_decoder_module(clk_enable="1")
    assert check_cdc_019(module, find_crossings(module)) == []
