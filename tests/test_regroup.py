"""Regrouping bit-blasted gate-level flops into registers (rtl-buddy-cdc#337).

:func:`rtl_buddy_cdc.regroup.regroup_flops` runs in the loader and
rebuilds one coarse flop cell per register from its one-bit
``$_DFF_*`` cells, keyed on the register's multi-bit netname plus
matching controls. These tests pin the grouping, the cases where it must
refuse (wrong merges are worse than no merge), mixed control signals,
partial buses, the combinational lanes in front of a register, the
functional gray fallback, and the conservative CDC-004 fallback for bits
that cannot be regrouped. The gate-level corpus
(``tests/test_gate_level_corpus.py``) checks the end-to-end verdicts.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.flops import find_flops, flop_enable_pin, flop_reset
from rtl_buddy_cdc.gate_eval import cone_tables
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Netname, Port
from rtl_buddy_cdc.regroup import ORPHAN_ATTR, REGROUPED_ATTR, regroup_flops
from rtl_buddy_cdc.rules import _bit_drivers, _is_gray_encoded_source

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CLK, CLK2, RST, RST2, EN, EN2 = 1, 2, 3, 4, 5, 6


def dff(
    name: str,
    d: Bit,
    q: Bit,
    *,
    type_: str = "$_DFF_PN0_",
    clk: Bit = CLK,
    rst: Bit = RST,
    en: Bit = EN,
    src: str | None = "top.sv:10.1-14.4",
) -> Cell:
    conns: dict[str, tuple[Bit, ...]] = {"C": (clk,), "D": (d,), "Q": (q,)}
    if type_.startswith(("$_DFF_P", "$_DFF_N")) and len(type_) > len("$_DFF_P_"):
        conns["R"] = (rst,)
    if type_.startswith(("$_SDFF", "$_DFFE_")) and type_.count("_") >= 3:
        if type_.startswith("$_SDFF") or len(type_) > len("$_DFFE_PP_"):
            conns["R"] = (rst,)
    if "E_" in type_:
        conns["E"] = (en,)
    attrs = {"src": src} if src is not None else {}
    return Cell(name=name, type=type_, connections=conns, attributes=attrs)


def module(
    cells: list[Cell],
    nets: dict[str, tuple[Bit, ...]],
    ports: dict[str, tuple[str, tuple[Bit, ...]]] | None = None,
) -> Module:
    return Module(
        name="top",
        ports={
            n: Port(name=n, direction=d, bits=b) for n, (d, b) in (ports or {}).items()
        },
        cells={c.name: c for c in cells},
        netnames={n: Netname(name=n, bits=b) for n, b in nets.items()},
    )


def bus(base: str, d0: int, q0: int, width: int, **kw) -> list[Cell]:
    """``width`` lanes, D nets d0.., Q nets q0.., cell names out of order."""
    return [
        dff(f"$auto$ff.cc:1:slice${base}{width - i}", d0 + i, q0 + i, **kw)
        for i in range(width)
    ]


def coarse(m: Module) -> dict[str, Cell]:
    return {n: c for n, c in m.cells.items() if not c.type.startswith("$_")}


# --- grouping ----------------------------------------------------------------


def test_bus_regroups_into_one_coarse_flop() -> None:
    cells = bus("a", 10, 20, 4)
    # Reset value per bit may differ: 4'b0101 is still one register.
    cells[0] = dff(cells[0].name, 10, 20, type_="$_DFF_PN1_")
    cells[2] = dff(cells[2].name, 12, 22, type_="$_DFF_PN1_")
    m = regroup_flops(module(cells, {"q": (20, 21, 22, 23), "d": (10, 11, 12, 13)}))
    assert set(m.cells) == {"q"}
    q = m.cells["q"]
    assert q.type == "$adff"
    assert q.connections["Q"] == (20, 21, 22, 23)
    assert q.connections["D"] == (10, 11, 12, 13)
    assert q.connections["CLK"] == (CLK,) and q.connections["ARST"] == (RST,)
    assert q.parameters["ARST_VALUE"] == "0101"  # MSB first
    assert int(q.parameters["ARST_POLARITY"], 2) == 0
    assert int(q.parameters["WIDTH"], 2) == 4
    assert q.attributes["src"] == "top.sv:10.1-14.4"
    assert sorted(q.attributes[REGROUPED_ATTR].split()) == sorted(c.name for c in cells)
    [flop] = find_flops(m)
    assert flop.width == 4
    rst = flop_reset(q)
    assert rst is not None and rst.role == "arst" and rst.polarity == 0


@pytest.mark.parametrize(
    ("gate", "coarse_type", "has_en", "reset_role"),
    [
        ("$_DFF_P_", "$dff", False, None),
        ("$_DFF_N_", "$dff", False, None),
        ("$_DFFE_PN_", "$dffe", True, None),
        ("$_DFFE_PP0P_", "$adffe", True, "arst"),
        ("$_SDFF_PP0_", "$sdff", False, "srst"),
        ("$_SDFFE_PN1P_", "$sdffe", True, "srst"),
        ("$_SDFFCE_PP0N_", "$sdffce", True, "srst"),
    ],
)
def test_every_family_maps_to_its_coarse_cell(
    gate: str, coarse_type: str, has_en: bool, reset_role: str | None
) -> None:
    cells = [
        Cell(
            name=f"f{i}",
            type=gate,
            connections={
                "C": (CLK,),
                "D": (10 + i,),
                "Q": (20 + i,),
                **({"R": (RST,)} if reset_role else {}),
                **({"E": (EN,)} if has_en else {}),
            },
        )
        for i in range(3)
    ]
    m = regroup_flops(module(cells, {"q": (20, 21, 22)}))
    q = m.cells["q"]
    assert q.type == coarse_type
    assert (flop_enable_pin(q) is not None) == has_en
    rst = flop_reset(q)
    assert (rst.role if rst else None) == reset_role
    # The coarse cell reads the same as each of its lanes did.
    for lane in cells:
        assert flop_enable_pin(lane) is not None if has_en else True
        lane_rst = flop_reset(lane)
        assert (lane_rst.role if lane_rst else None) == reset_role
        if rst and lane_rst:
            assert (rst.polarity, rst.kind) == (lane_rst.polarity, lane_rst.kind)


def test_dffsr_and_aldff_families_regroup() -> None:
    sr = [
        Cell(
            name=f"s{i}",
            type="$_DFFSR_PNP_",
            connections={
                "C": (CLK,),
                "S": (EN,),
                "R": (RST,),
                "D": (10 + i,),
                "Q": (20 + i,),
            },
        )
        for i in range(2)
    ]
    al = [
        Cell(
            name=f"l{i}",
            type="$_ALDFF_PP_",
            connections={
                "C": (CLK,),
                "L": (EN,),
                "AD": (40 + i,),
                "D": (30 + i,),
                "Q": (50 + i,),
            },
        )
        for i in range(2)
    ]
    m = regroup_flops(module(sr + al, {"s": (20, 21), "l": (50, 51)}))
    assert m.cells["s"].type == "$dffsr"
    assert m.cells["s"].connections["CLR"] == (RST, RST)
    assert m.cells["l"].type == "$aldff"
    assert m.cells["l"].connections["AD"] == (40, 41)


def test_coarse_netlist_is_returned_unchanged() -> None:
    m = netlist.load(FIXTURES / "bad_bus_crossing" / "bad_bus_crossing.json")
    assert regroup_flops(m) is m


def test_lane_order_follows_the_netname_not_cell_names() -> None:
    cells = bus("a", 10, 20, 3)
    m = regroup_flops(module(cells, {"q": (22, 20, 21)}))
    assert m.cells["q"].connections["Q"] == (22, 20, 21)
    assert m.cells["q"].connections["D"] == (12, 10, 11)


def test_port_register_keeps_the_port_name() -> None:
    m = regroup_flops(
        module(bus("a", 10, 20, 2), {"q": (20, 21)}, {"q": ("output", (20, 21))})
    )
    assert set(coarse(m)) == {"q"}


def test_alias_port_loses_to_the_register_name() -> None:
    m = regroup_flops(
        module(
            bus("a", 10, 20, 2),
            {"sync_q": (20, 21), "dst_data": (20, 21)},
            {"dst_data": ("output", (20, 21))},
        )
    )
    assert set(coarse(m)) == {"sync_q"}


def test_one_bit_aliases_do_not_block_the_register() -> None:
    # assign d0 = data[0]; ... (bad_sliced_bus_reconvergence)
    nets = {"data": (20, 21, 22), "d0": (20,), "d1": (21,), "d2": (22,)}
    m = regroup_flops(module(bus("a", 10, 20, 3), nets))
    assert set(coarse(m)) == {"data"}


def test_split_netnames_rebuild_the_vector() -> None:
    # ``splitnets`` leaves q[0], q[1], ... and no ``q``.
    nets = {f"q[{i}]": (20 + i,) for i in range(4)}
    m = regroup_flops(module(bus("a", 10, 20, 4), nets))
    assert set(coarse(m)) == {"q"}
    assert m.cells["q"].connections["Q"] == (20, 21, 22, 23)


def test_sparse_split_names_are_not_a_vector() -> None:
    nets = {"q[0]": (20,), "q[9]": (21,)}
    m = regroup_flops(module(bus("a", 10, 20, 2), nets))
    assert coarse(m) == {}


# --- refusing to group -------------------------------------------------------


def test_scalar_registers_concatenated_into_a_port_stay_apart() -> None:
    # assign sync_out = {d1_s, d0_s}; two one-bit registers.
    nets = {"d0_s": (20,), "d1_s": (21,), "sync_out": (20, 21)}
    m = regroup_flops(
        module(bus("a", 10, 20, 2), nets, {"sync_out": ("output", (20, 21))})
    )
    assert coarse(m) == {}
    assert not any(ORPHAN_ATTR in c.attributes for c in m.cells.values())


def test_concatenation_wire_does_not_merge_two_registers() -> None:
    cells = bus("a", 10, 20, 2) + bus("b", 12, 22, 2)
    nets = {"a_q": (20, 21), "b_q": (22, 23), "bus": (20, 21, 22, 23)}
    m = regroup_flops(module(cells, nets))
    assert set(coarse(m)) == {"a_q", "b_q"}


def test_hidden_netnames_never_group() -> None:
    nets = {"$auto$wire$1": (20, 21, 22)}
    m = regroup_flops(module(bus("a", 10, 20, 3), nets))
    assert coarse(m) == {}
    # No name of their own: tagged for CDC-004's conservative fallback.
    assert all(ORPHAN_ATTR in c.attributes for c in m.cells.values())


def test_merged_bit_refuses_the_whole_register() -> None:
    # opt_merge folded q[0] and q[2] into one flop.
    cells = bus("a", 10, 20, 2)
    m = regroup_flops(module(cells, {"q": (20, 21, 20)}))
    assert coarse(m) == {}
    assert all(ORPHAN_ATTR in c.attributes for c in m.cells.values())


def test_single_flop_is_left_alone() -> None:
    m = module(bus("a", 10, 20, 1), {"q": (20,)})
    assert regroup_flops(m) is m


def test_non_flop_and_malformed_cells_are_ignored() -> None:
    latch = Cell("l", "$_DLATCH_P_", {"E": (CLK,), "D": (10,), "Q": (20,)})
    wide = Cell("w", "$_DFF_P_", {"C": (CLK,), "D": (11, 12), "Q": (21, 22)})
    m = module([latch, wide], {"q": (20, 21, 22)})
    assert regroup_flops(m) is m


def test_two_drivers_on_one_q_net_are_not_grouped() -> None:
    cells = [dff("x", 10, 20), dff("y", 11, 20), dff("z", 12, 21)]
    m = regroup_flops(module(cells, {"q": (20, 21)}))
    assert coarse(m) == {}


# --- mixed control signals ---------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"clk": CLK2},
        {"rst": RST2},
        {"type_": "$_DFF_NN0_"},  # other clock edge
        {"type_": "$_DFF_PP0_"},  # other reset polarity
        {"src": "top.sv:20.1-24.4"},  # assigned in another always block
    ],
    ids=["clock", "reset-net", "edge", "reset-polarity", "always-block"],
)
def test_mixed_controls_split_the_netname(kw: dict) -> None:
    cells = bus("a", 10, 20, 2) + bus("b", 12, 22, 2, **kw)
    m = regroup_flops(module(cells, {"q": (20, 21, 22, 23)}))
    assert set(coarse(m)) == {"q[1:0]", "q[3:2]"}
    assert m.cells["q[1:0]"].connections["Q"] == (20, 21)
    assert m.cells["q[3:2]"].connections["Q"] == (22, 23)


def test_different_enables_split_and_a_singleton_stays_a_gate() -> None:
    cells = bus("a", 10, 20, 2, type_="$_DFFE_PP_") + [
        dff("odd", 12, 22, type_="$_DFFE_PP_", en=EN2)
    ]
    m = regroup_flops(module(cells, {"q": (20, 21, 22)}))
    assert set(coarse(m)) == {"q[1:0]"}
    assert m.cells["q[1:0]"].connections["EN"] == (EN,)
    assert m.cells["odd"].type == "$_DFFE_PP_"
    # A leftover lane is a bit of a register that could not be rebuilt.
    assert ORPHAN_ATTR in m.cells["odd"].attributes
    assert ORPHAN_ATTR not in m.cells["q[1:0]"].attributes


def test_reset_flop_and_plain_flop_do_not_mix() -> None:
    cells = bus("a", 10, 20, 2) + bus("b", 12, 22, 2, type_="$_DFF_P_")
    m = regroup_flops(module(cells, {"q": (20, 21, 22, 23)}))
    assert {c.type for c in coarse(m).values()} == {"$adff", "$dff"}


# --- partial buses -----------------------------------------------------------


def test_partial_bus_groups_the_surviving_lanes() -> None:
    # Bits 2 and 4 of q were optimised to constants / other logic.
    and_cell = Cell("g", "$_AND_", {"A": (1,), "B": (2,), "Y": (24,)})
    cells = [dff("f0", 10, 20), dff("f1", 11, 21), dff("f3", 13, 23), and_cell]
    m = regroup_flops(module(cells, {"q": (20, 21, "0", 23, 24)}))
    assert set(coarse(m)) == {"q"}
    assert m.cells["q"].connections["Q"] == (20, 21, 23)


def test_partial_split_names_are_non_contiguous() -> None:
    cells = bus("a", 10, 20, 3) + [dff("x", 13, 23, clk=CLK2)]
    m = regroup_flops(module(cells, {"q": (20, 23, 21, 22)}))
    assert set(coarse(m)) == {"q[0,2,3]"}


def test_name_clash_with_an_existing_cell_is_suffixed() -> None:
    other = Cell("q", "$_AND_", {"A": (1,), "B": (2,), "Y": (99,)})
    m = regroup_flops(module([*bus("a", 10, 20, 2), other], {"q": (20, 21)}))
    assert m.cells["q$regroup"].type == "$adff"
    assert m.cells["q"] is other


# --- combinational lanes -----------------------------------------------------


def _mux(name: str, a: Bit, b: Bit, s: Bit, y: Bit) -> Cell:
    return Cell(name, "$_MUX_", {"A": (a,), "B": (b,), "S": (s,), "Y": (y,)})


def _xor(name: str, a: Bit, b: Bit, y: Bit) -> Cell:
    return Cell(name, "$_XOR_", {"A": (a,), "B": (b,), "Y": (y,)})


def test_enable_mux_lanes_become_one_mux_and_the_walk_continues() -> None:
    # q <= en ? (b ^ (b >> 1)) : q, bit-blasted.
    flops = [dff(f"f{i}", 10 + i, 20 + i) for i in range(3)]
    muxes = [_mux(f"m{i}", 20 + i, 30 + i, EN, 10 + i) for i in range(3)]
    xors = [
        _xor("x0", 40, 41, 30),
        _xor("x1", 41, 42, 31),
        _xor("x2", 42, "0", 32),
    ]
    m = regroup_flops(module(flops + muxes + xors, {"q": (20, 21, 22)}))
    kinds = Counter(c.type for c in m.cells.values())
    assert kinds == Counter({"$adff": 1, "$mux": 1, "$xor": 1})
    [mux] = [c for c in m.cells.values() if c.type == "$mux"]
    assert mux.connections["S"] == (EN,)
    assert mux.connections["Y"] == (10, 11, 12)
    [xor] = [c for c in m.cells.values() if c.type == "$xor"]
    assert xor.connections["A"] == (40, 41, 42)
    assert xor.connections["B"] == (41, 42, "0")
    # ... which is exactly the shape the RTL gray detector matches.
    [flop] = find_flops(m)
    src = m.cells["q"]
    assert src is flop.cell
    assert _is_gray_encoded_source(m, flop, _bit_drivers(m))


def test_mux_lanes_with_different_selects_stay_gates() -> None:
    flops = [dff(f"f{i}", 10 + i, 20 + i) for i in range(2)]
    muxes = [_mux("m0", 20, 30, EN, 10), _mux("m1", 21, 31, EN2, 11)]
    m = regroup_flops(module(flops + muxes, {"q": (20, 21)}))
    assert {c.type for c in m.cells.values()} == {"$adff", "$_MUX_"}


def test_mixed_gate_types_stay_gates() -> None:
    flops = [dff(f"f{i}", 10 + i, 20 + i) for i in range(2)]
    gates = [
        Cell("a", "$_AND_", {"A": (30,), "B": (31,), "Y": (10,)}),
        Cell("o", "$_OR_", {"A": (30,), "B": (31,), "Y": (11,)}),
    ]
    m = regroup_flops(module(flops + gates, {"q": (20, 21)}))
    assert {c.type for c in m.cells.values()} == {"$adff", "$_AND_", "$_OR_"}


def test_not_and_buf_lanes_merge() -> None:
    flops = [dff(f"f{i}", 10 + i, 20 + i) for i in range(2)]
    gates = [
        Cell("n0", "$_NOT_", {"A": (30,), "Y": (10,)}),
        Cell("n1", "$_NOT_", {"A": (31,), "Y": (11,)}),
        Cell("b0", "$_BUF_", {"A": (40,), "Y": (30,)}),
        Cell("b1", "$_BUF_", {"A": (41,), "Y": (31,)}),
    ]
    m = regroup_flops(module(flops + gates, {"q": (20, 21)}))
    assert {c.type for c in m.cells.values()} == {"$adff", "$not", "$pos"}


def test_shared_gate_is_merged_once() -> None:
    # Both lanes of D are the same gate's output: not one bit-blasted cell.
    flops = [dff("f0", 10, 20), dff("f1", 10, 21)]
    gate = Cell("g", "$_AND_", {"A": (1,), "B": (2,), "Y": (10,)})
    m = regroup_flops(module([*flops, gate], {"q": (20, 21)}))
    assert m.cells["g"] is gate


# --- gate_eval ---------------------------------------------------------------


def _drivers(cells: list[Cell]) -> tuple[Module, dict]:
    m = module(cells, {})
    return m, _bit_drivers(m)


def test_cone_tables_compare_functions() -> None:
    # y1 = a ^ b ; y2 = (a | b) & ~(a & b)  -> the same function.
    cells = [
        Cell("x", "$_XOR_", {"A": (1,), "B": (2,), "Y": (10,)}),
        Cell("o", "$_OR_", {"A": (1,), "B": (2,), "Y": (11,)}),
        Cell("n", "$_NAND_", {"A": (1,), "B": (2,), "Y": (12,)}),
        Cell("y", "$_AND_", {"A": (11,), "B": (12,), "Y": (13,)}),
        Cell("z", "$_XNOR_", {"A": (1,), "B": (2,), "Y": (14,)}),
    ]
    m, drv = _drivers(cells)
    tables = cone_tables(m, [(10, 13, 14)], drv)
    assert tables is not None
    [(x, y, z)] = tables
    assert x == y != z


@pytest.mark.parametrize(
    ("ctype", "pins", "fn"),
    [
        ("$_BUF_", "A", lambda a, b, c, d: a),
        ("$_NOT_", "A", lambda a, b, c, d: 1 - a),
        ("$_NOR_", "AB", lambda a, b, c, d: 1 - (a | b)),
        ("$_ANDNOT_", "AB", lambda a, b, c, d: a & (1 - b)),
        ("$_ORNOT_", "AB", lambda a, b, c, d: a | (1 - b)),
        ("$_MUX_", "ABS", lambda a, b, s, d: b if s else a),
        ("$_NMUX_", "ABS", lambda a, b, s, d: 1 - (b if s else a)),
        ("$_AOI3_", "ABC", lambda a, b, c, d: 1 - ((a & b) | c)),
        ("$_OAI3_", "ABC", lambda a, b, c, d: 1 - ((a | b) & c)),
        ("$_AOI4_", "ABCD", lambda a, b, c, d: 1 - ((a & b) | (c & d))),
        ("$_OAI4_", "ABCD", lambda a, b, c, d: 1 - ((a | b) & (c | d))),
    ],
)
def test_cone_tables_gate_semantics(ctype: str, pins: str, fn) -> None:
    cell = Cell("g", ctype, {**{p: (1 + i,) for i, p in enumerate(pins)}, "Y": (9,)})
    m, drv = _drivers([cell])
    tables = cone_tables(m, [(9,)], drv)
    assert tables is not None
    [(t,)] = tables
    # Leaves are numbered in discovery order; recover each input's
    # pattern from a direct table of the input nets.
    leaves = cone_tables(m, [(9,), tuple(1 + i for i in range(len(pins)))], drv)
    assert leaves is not None
    [(t,), ins] = leaves
    n = len(pins)
    for j in range(1 << n):
        vals = [(p >> j) & 1 for p in ins] + [0] * (4 - n)
        assert (t >> j) & 1 == fn(*vals)


def test_cone_tables_coarse_lane_cells_and_constants() -> None:
    cells = [
        Cell(
            "x",
            "$xor",
            {"A": (1, 2), "B": (2, "1"), "Y": (10, 11)},
            {"A_WIDTH": "10", "B_WIDTH": "10", "Y_WIDTH": "10"},
        ),
        Cell("m", "$mux", {"A": (1, 2), "B": (2, 1), "S": (3,), "Y": (12, 13)}),
        Cell("n", "$not", {"A": (11,), "Y": (14,)}),
    ]
    m, drv = _drivers(cells)
    tables = cone_tables(m, [(14, 2), (12,)], drv)
    assert tables is not None
    [(not_b_xor_1, b), _] = tables
    assert not_b_xor_1 == b  # ~(b ^ 1) == b


def test_cone_tables_give_up() -> None:
    loop = [
        Cell("a", "$_AND_", {"A": (1,), "B": (11,), "Y": (10,)}),
        Cell("b", "$_OR_", {"A": (10,), "B": (2,), "Y": (11,)}),
    ]
    m, drv = _drivers(loop)
    assert cone_tables(m, [(10,)], drv) is None
    add = [Cell("p", "$add", {"A": (1,), "B": (2,), "Y": (10,)})]
    m, drv = _drivers(add)
    assert cone_tables(m, [(10,)], drv) is None
    xz = [Cell("x", "$_AND_", {"A": (1,), "B": ("x",), "Y": (10,)})]
    m, drv = _drivers(xz)
    assert cone_tables(m, [(10,)], drv) is None
    wide = [Cell("w", "$and", {"A": (1,), "B": (2, 3), "Y": (10, 11)})]
    m, drv = _drivers(wide)
    assert cone_tables(m, [(10,)], drv) is None
    wsel = [Cell("w", "$mux", {"A": (1,), "B": (2,), "S": (3, 4), "Y": (10,)})]
    m, drv = _drivers(wsel)
    assert cone_tables(m, [(10,)], drv) is None
    many = [
        Cell(f"g{i}", "$_AND_", {"A": (100 + i,), "B": (200 + i,), "Y": (10 + i,)})
        for i in range(6)
    ]
    m, drv = _drivers(many)
    assert cone_tables(m, [tuple(10 + i for i in range(6))], drv) is None
    assert cone_tables(m, [(10, 11)], drv, max_cells=1) is None


# --- end to end ---------------------------------------------------------------

_RUNNER = CliRunner()


def _fired(net: Path, sdc: Path) -> list[dict]:
    result = _RUNNER.invoke(
        app, ["analyze", "-n", str(net), "-s", str(sdc), "-f", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.stdout)["violations"]


def _rewrite_netnames(src: Path, dst: Path, fn) -> None:
    data = json.loads(src.read_text())
    for mod in data["modules"].values():
        mod["netnames"] = fn(mod["netnames"])
    dst.write_text(json.dumps(data))


BUS = FIXTURES / "bad_bus_crossing"


def test_gate_level_bus_reports_cdc004_on_the_register() -> None:
    v = _fired(BUS / "bad_bus_crossing.gates.json", BUS / "bad_bus_crossing.sdc")
    assert [x["rule_id"] for x in v] == ["CDC-004"]
    assert "src: src_q" in v[0]["message"]
    assert "dst flop: sync_stage0" in v[0]["message"]


def test_split_netnames_still_report_cdc004(tmp_path: Path) -> None:
    def split(nets: dict) -> dict:
        out = {}
        for name, nd in nets.items():
            if len(nd["bits"]) > 1 and name not in ("src_data", "dst_data"):
                for i, b in enumerate(nd["bits"]):
                    out[f"{name}[{i}]"] = {**nd, "bits": [b]}
            else:
                out[name] = nd
        return out

    net = tmp_path / "split.json"
    _rewrite_netnames(BUS / "bad_bus_crossing.gates.json", net, split)
    v = _fired(net, BUS / "bad_bus_crossing.sdc")
    assert [(x["rule_id"], x["severity"]) for x in v] == [("CDC-004", "error")]


def _hide_internal(nets: dict) -> dict:
    """Synthesis dropped every internal name; port names survive."""
    keep = ("src_clk", "dst_clk", "rst_n", "src_data", "dst_data")
    return {(n if n in keep else f"$abc$hidden${n}"): nd for n, nd in nets.items()}


def test_lost_register_names_stay_conservative(tmp_path: Path) -> None:
    """Synthesis dropped the internal register names, so ``src_q`` and
    ``sync_stage0`` cannot be regrouped (``sync_stage1`` still is, by
    the ``dst_data`` port it drives). The verdict stays conservative:
    the per-bit chains into the regrouped last stage read as CDC-001,
    and CDC-004's fallback reports the orphaned source bits as a
    possible bus (a warning) — never silence."""
    net = tmp_path / "hidden.json"
    _rewrite_netnames(BUS / "bad_bus_crossing.gates.json", net, _hide_internal)
    v = _fired(net, BUS / "bad_bus_crossing.sdc")
    assert Counter((x["rule_id"], x["severity"]) for x in v) == Counter(
        {("CDC-001", "error"): 8, ("CDC-004", "warning"): 1}
    )
    [orphan] = [x for x in v if x["rule_id"] == "CDC-004"]
    assert "could not be rebuilt" in orphan["message"]


def test_lost_names_without_port_alias_still_report_the_bus(tmp_path: Path) -> None:
    """Every register name gone and each bit a clean 2FF chain: without
    the fallback this is silent, exactly the CDC-004 bug."""

    def hide_all(nets: dict) -> dict:
        out = _hide_internal(nets)
        out["$abc$hidden$dst_data"] = out.pop("dst_data")
        return out

    net = tmp_path / "hidden.json"
    _rewrite_netnames(BUS / "bad_bus_crossing.gates.json", net, hide_all)
    v = _fired(net, BUS / "bad_bus_crossing.sdc")
    assert [(x["rule_id"], x["severity"]) for x in v] == [("CDC-004", "warning")]


def test_lost_names_with_gated_lanes_stay_quiet(tmp_path: Path) -> None:
    """The fallback keeps the gated-bus exemption: every lane of
    good_dffe_gated_bus_crossing loads on a synchronised enable."""
    gd = FIXTURES / "good_dffe_gated_bus_crossing"

    def hide(nets: dict) -> dict:
        return {
            (f"$abc$hidden${n}" if n in ("src_data_q", "dst_data_q") else n): nd
            for n, nd in nets.items()
        }

    net = tmp_path / "hidden.json"
    _rewrite_netnames(gd / "good_dffe_gated_bus_crossing.gates.json", net, hide)
    sdc = gd / "good_dffe_gated_bus_crossing.sdc"
    assert "CDC-004" not in {x["rule_id"] for x in _fired(net, sdc)}


def test_orphans_without_src_are_not_grouped(tmp_path: Path) -> None:
    def strip(path: Path) -> None:
        data = json.loads(path.read_text())
        for mod in data["modules"].values():
            for cell in mod["cells"].values():
                cell["attributes"].pop("src", None)
            mod["netnames"] = {
                (n if n.endswith("clk") or n in ("rst_n",) else f"$h${n}"): nd
                for n, nd in mod["netnames"].items()
            }
        path.write_text(json.dumps(data))

    net = tmp_path / "nosrc.json"
    net.write_text((BUS / "bad_bus_crossing.gates.json").read_text())
    strip(net)
    assert _fired(net, BUS / "bad_bus_crossing.sdc") == []


def test_functional_gray_after_abc() -> None:
    """``abc`` re-derives the gray code with no ``b ^ (b >> 1)`` XOR;
    the functional fallback still recognises it (no CDC-012)."""
    gc = FIXTURES / "good_gray_counter_crossing"
    net = gc / "good_gray_counter_crossing.synth.json"
    m = netlist.load(net)
    drv = _bit_drivers(m)
    gray = next(f for f in find_flops(m) if f.name == "gray")
    assert _is_gray_encoded_source(m, gray, drv)
    binary = next(f for f in find_flops(m) if f.name == "bin")
    assert not _is_gray_encoded_source(m, binary, drv)
    assert _fired(net, gc / "good_gray_counter_crossing.sdc") == []


def test_cdc019_groups_a_bit_blasted_decoder() -> None:
    fx = FIXTURES / "bad_onehot_decode_independent_sync"
    v = _fired(
        fx / "bad_onehot_decode_independent_sync.gates.json",
        fx / "bad_onehot_decode_independent_sync.sdc",
    )
    assert [x["rule_id"] for x in v] == ["CDC-019"]
    assert "driving 'one_hot'" in v[0]["message"]


def test_loader_regroups_every_module() -> None:
    top, _bbs = netlist.load_with_blackboxes(BUS / "bad_bus_crossing.gates.json")
    assert {c.type for c in top.cells.values()} == {"$adff"}
    assert set(top.cells) == {"src_q", "sync_stage0", "sync_stage1"}
