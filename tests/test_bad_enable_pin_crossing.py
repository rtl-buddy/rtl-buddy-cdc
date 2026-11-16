"""A crossing through a flop's enable or sync-reset pin (rtl-buddy-cdc#375).

``find_crossings`` used to end its walk on flop ``D`` pins only. After
``proc`` a flop's enable is a hold mux and its synchronous reset a
constant-leg mux, both on ``D``, so a foreign signal on either was in the
``D`` cone and its crossing was found. ``opt_dff`` folds both muxes into
the flop (``$dffe`` ``EN``, ``$sdff`` ``SRST``; ``E`` / ``R`` at gate
level), and the crossing silently disappeared from optimised and
gate-level netlists. Both pins are now capture inputs of the walk, at the
depth of the mux they were folded from.

- ``bad_enable_pin_crossing``: the issue's reproducer, committed in the
  ``proc`` (``$mux`` on D), ``opt_dff`` (``$dffe``) and gate-level
  (``$_DFFE_PP_``) forms. One crossing and CDC-001 in all three.
- Hand-built cells pin the per-family pin and depth table, the sync-reset
  pin, the port- and boundary-sourced walks and the ``-through`` walker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import domain as domain_mod, netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import capture_control_pins, find_crossings
from rtl_buddy_cdc.netlist import BoundarySummary, Cell, Module, Port, PortBoundary

NAME = "bad_enable_pin_crossing"
FIX = Path(__file__).parent / "fixtures" / NAME
FORMS = ["json", "optdff.json", "synth.json"]


def _analyze(suffix: str) -> dict:
    r = CliRunner().invoke(
        app,
        [
            "analyze",
            "-n",
            str(FIX / f"{NAME}.{suffix}"),
            "-s",
            str(FIX / f"{NAME}.sdc"),
            "-f",
            "json",
        ],
    )
    assert r.exit_code == 1, r.output
    return json.loads(r.stdout)


def test_netlist_forms_carry_the_shapes_under_test() -> None:
    types = {
        suffix: {c.type for c in netlist.load(FIX / f"{NAME}.{suffix}").cells.values()}
        for suffix in FORMS
    }
    assert "$mux" in types["json"] and "$dffe" not in types["json"]
    assert "$dffe" in types["optdff.json"] and "$mux" not in types["optdff.json"]
    assert "$_DFFE_PP_" in types["synth.json"]


@pytest.mark.parametrize("suffix", FORMS)
def test_enable_crossing_is_found_in_every_form(suffix: str) -> None:
    report = _analyze(suffix)
    assert report["summary"]["crossings"] == 1
    (c,) = report["crossings"]
    assert (c["src_clock"], c["dst_clock"]) == ("clk_a", "clk_b")
    # The folded hold mux still counts as the one cell on the path.
    assert (c["width"], c["min_hops"]) == (1, 1)
    assert [v["rule_id"] for v in report["violations"]] == ["CDC-001"]


# --- the capture-control pin table -----------------------------------------


def _cell(ctype: str, **conns: tuple) -> Cell:
    return Cell(name="f", type=ctype, connections=dict(conns))


@pytest.mark.parametrize(
    ("ctype", "conns", "expected"),
    [
        ("$dff", {"CLK": (1,), "D": (2,), "Q": (3,)}, ()),
        ("$dffe", {"CLK": (1,), "EN": (4,)}, (("EN", 1),)),
        ("$adffe", {"CLK": (1,), "ARST": (5,), "EN": (4,)}, (("EN", 1),)),
        # An async reset is not a capture input.
        ("$adff", {"CLK": (1,), "ARST": (5,)}, ()),
        ("$sdff", {"CLK": (1,), "SRST": (5,)}, (("SRST", 1),)),
        # Reset has priority: its mux is next to D, the enable's behind it.
        ("$sdffe", {"CLK": (1,), "SRST": (5,), "EN": (4,)}, (("EN", 2), ("SRST", 1))),
        # Reset only while enabled: the enable's mux is next to D.
        ("$sdffce", {"CLK": (1,), "SRST": (5,), "EN": (4,)}, (("EN", 1), ("SRST", 2))),
        ("$_DFFE_PP_", {"C": (1,), "E": (4,)}, (("E", 1),)),
        ("$_DFFE_PN0P_", {"C": (1,), "R": (5,), "E": (4,)}, (("E", 1),)),
        ("$_SDFF_PP0_", {"C": (1,), "R": (5,)}, (("R", 1),)),
        ("$_SDFFE_PP0P_", {"C": (1,), "R": (5,), "E": (4,)}, (("E", 2), ("R", 1))),
        ("$_SDFFCE_PP0P_", {"C": (1,), "R": (5,), "E": (4,)}, (("E", 1), ("R", 2))),
        # Not flops.
        ("$dlatch", {"EN": (4,)}, ()),
        ("$mux", {"S": (4,)}, ()),
    ],
)
def test_capture_control_pins(ctype: str, conns: dict, expected: tuple) -> None:
    assert capture_control_pins(_cell(ctype, **conns)) == expected


# --- hand-built walks -------------------------------------------------------

# Bits: 2 = clk_a, 3 = clk_b, 4 = a.Q (clk_a), 5 = b.D, 6 = b.Q,
# 7 = buffered a.Q, 8 = port p, 9 = boundary output.


def _ff(name: str, ctype: str, clk: int, **conns: tuple) -> Cell:
    pin = "C" if ctype.startswith("$_") else "CLK"
    return Cell(name=name, type=ctype, connections={pin: (clk,), **conns})


def _module(dst: Cell, *extra: Cell, ports: tuple[Port, ...] = ()) -> Module:
    cells = {
        "a": _ff("a", "$dff", 2, D=(10,), Q=(4,)),
        "buf": Cell(name="buf", type="$not", connections={"A": (4,), "Y": (7,)}),
        dst.name: dst,
        **{c.name: c for c in extra},
    }
    all_ports = {
        "clk_a": Port(name="clk_a", direction="input", bits=(2,)),
        "clk_b": Port(name="clk_b", direction="input", bits=(3,)),
        **{p.name: p for p in ports},
    }
    return Module(name="m", ports=all_ports, cells=cells, netnames={})


@pytest.mark.parametrize(
    ("ctype", "pin", "hops"),
    [
        ("$sdff", "SRST", 1),
        ("$_SDFF_PP0_", "R", 1),
        ("$sdffe", "EN", 2),
        ("$sdffce", "SRST", 2),
        ("$dffe", "EN", 1),
    ],
)
def test_crossing_through_control_pin(ctype: str, pin: str, hops: int) -> None:
    other = {"SRST": (11,), "EN": (12,), "R": (11,)}
    conns = {"D": (5, 13), "Q": (6, 14)}
    for p, _ in capture_control_pins(_cell(ctype, **other)):
        conns[p] = other[p]
    conns[pin] = (4,)
    (c,) = find_crossings(_module(_ff("b", ctype, 3, **conns)))
    assert (c.src_flop.name, c.dst_flop.name) == ("a", "b")
    # Every D lane is reached through the pin, like the mux select was,
    # but from one source bit: a single-bit crossing (rtl-buddy-cdc#390).
    assert (c.min_hops, c.width) == (hops, 1)


def test_control_pin_counts_cells_in_front_of_it() -> None:
    dst = _ff("b", "$dffe", 3, D=(5,), Q=(6,), EN=(7,))
    (c,) = find_crossings(_module(dst))
    assert c.min_hops == 2
    # The budget covers the folded mux too: one cell plus the mux is two.
    assert find_crossings(_module(dst), max_hops=1) == []
    assert len(find_crossings(_module(dst), max_hops=2)) == 1


def test_async_reset_pin_is_not_a_crossing() -> None:
    dst = _ff("b", "$adff", 3, D=(5,), Q=(6,), ARST=(4,))
    assert find_crossings(_module(dst)) == []


def test_same_domain_enable_is_not_a_crossing() -> None:
    dst = _ff("b", "$dffe", 2, D=(5,), Q=(6,), EN=(4,))
    assert find_crossings(_module(dst)) == []


def test_constant_d_reports_width_one() -> None:
    dst = _ff("b", "$dffe", 3, D=("1",), Q=(6,), EN=(4,))
    (c,) = find_crossings(_module(dst))
    assert c.width == 1


# --- one source bit fanned out to several lanes (rtl-buddy-cdc#390) -------


def _wide_src(*extra: Cell, dst: Cell, ports: tuple[Port, ...] = ()) -> Module:
    """``_module`` with ``a`` two bits wide: ``Q = (4, 15)``."""
    module = _module(dst, *extra, ports=ports)
    module.cells["a"] = _ff("a", "$dff", 2, D=(10, 16), Q=(4, 15))
    return module


def test_one_bit_of_a_wide_source_on_an_enable_is_single_bit() -> None:
    dst = _ff("b", "$dffe", 3, D=(5, 13), Q=(6, 14), EN=(4,))
    (c,) = find_crossings(_wide_src(dst=dst))
    assert c.width == 1


def test_two_bits_of_a_source_are_a_bus() -> None:
    """Bit 4 on the enable, bit 15 on a ``D`` lane: two source bits."""
    dst = _ff("b", "$dffe", 3, D=(15, 13), Q=(6, 14), EN=(4,))
    (c,) = find_crossings(_wide_src(dst=dst))
    assert c.width == 2


def test_lane_aligned_bus_keeps_its_width() -> None:
    dst = _ff("b", "$dff", 3, D=(4, 15), Q=(6, 14))
    (c,) = find_crossings(_wide_src(dst=dst))
    assert c.width == 2


def test_one_bit_replicated_on_every_lane_is_single_bit() -> None:
    """``b <= {~a, ~a}`` through two inverters: two ``D`` bits, one source
    bit."""
    dst = _ff("b", "$dff", 3, D=(7, 19), Q=(6, 14))
    inv = Cell(name="inv2", type="$not", connections={"A": (4,), "Y": (19,)})
    (c,) = find_crossings(_module(dst, inv))
    assert (c.width, c.min_hops) == (1, 1)


def test_one_bit_of_a_wide_port_on_an_enable_is_single_bit() -> None:
    dst = _ff("b", "$dffe", 3, D=(5, 13), Q=(6, 14), EN=(8,))
    p = Port(name="p", direction="input", bits=(8, 17))
    (c,) = find_crossings(_module(dst, ports=(p,)), port_clock={"p": "clk_a"})
    assert (c.src_port, c.width) == ("p", 1)


def test_one_bit_of_a_wide_boundary_port_on_an_enable_is_single_bit() -> None:
    dst = _ff("b", "$dffe", 3, D=(5, 13), Q=(6, 14), EN=(9,))
    inst = Cell(name="u", type="sub", connections={"o": (9, 18)})
    summary = BoundarySummary(
        module="sub",
        clock="clk_a",
        ports={
            "o": PortBoundary(port="o", src_clock="clk_a", synchronised=False, width=2)
        },
    )
    crossings = find_crossings(_module(dst, inst), boundaries={"u": summary})
    (c,) = [c for c in crossings if c.src_boundary is not None]
    assert c.width == 1


def test_wide_source_walk_expands_each_bit_at_most_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-bit test rides on the crossing walk itself: each bit
    carries the one source bit it was reached from, or "several", and is
    expanded again only when that label rises — at most twice per source,
    whatever the source's width (rtl-buddy-cdc#390). A per-source-bit
    re-walk would expand the shared adder cone below once per bit.

    ``a`` (clk_a, 64 bits): ``a[0]`` enables ``b`` (clk_b), so ``b`` is
    reached from one bit; every bit feeds a 64-bit ``$add`` (each input
    reaches every output) into ``c`` (clk_b).
    """
    w = 64
    a_q = tuple(range(100, 100 + w))
    add_y = tuple(range(300, 300 + w))
    cells = [
        _ff("a", "$dff", 2, D=tuple(range(500, 500 + w)), Q=a_q),
        Cell(
            name="add",
            type="$add",
            connections={"A": a_q, "B": a_q[::-1], "Y": add_y},
        ),
        _ff("c", "$dff", 3, D=add_y, Q=tuple(range(700, 700 + w))),
        _ff("b", "$dffe", 3, D=(5, 13), Q=(6, 14), EN=(a_q[0],)),
    ]
    module = Module(
        name="m",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(2,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(3,)),
        },
        cells={c.name: c for c in cells},
        netnames={},
    )
    calls: dict[tuple[str, str, int], int] = {}
    real = domain_mod._lane_targets

    def counting(cell: Cell, port: str, idx: int):  # noqa: ANN202
        key = (cell.name, port, idx)
        calls[key] = calls.get(key, 0) + 1
        return real(cell, port, idx)

    monkeypatch.setattr(domain_mod, "_lane_targets", counting)
    widths = {c.dst_flop.name: c.width for c in find_crossings(module)}
    assert widths == {"b": 1, "c": w}
    # Every consumer site of the one source is expanded at most twice.
    assert calls and max(calls.values()) <= 2
    assert sum(calls.values()) <= 2 * 2 * w


def test_second_source_bit_arriving_late_is_replayed() -> None:
    """``x = a[1] & ~~a[0]``: the walk reaches ``x`` from ``a[1]`` in one
    cell and expands it before ``a[0]`` arrives two cells later. The rise
    to "several source bits" is replayed through ``x`` (and ``z = ~x``)
    afterwards, so ``b`` — whose lanes are ``x`` and ``z`` — is a 2-bit
    crossing, not a single-bit one."""
    cells = [
        Cell(name="n1", type="$not", connections={"A": (15,), "Y": (30,)}),
        Cell(name="n2", type="$not", connections={"A": (30,), "Y": (31,)}),
        Cell(name="x", type="$and", connections={"A": (4,), "B": (31,), "Y": (32,)}),
        Cell(name="z", type="$not", connections={"A": (32,), "Y": (33,)}),
        _ff("b", "$dff", 3, D=(32, 33), Q=(6, 14)),
    ]
    (c,) = find_crossings(_wide_src(*cells, dst=cells[-1]))
    assert (c.width, c.min_hops) == (2, 1)


def test_port_sourced_enable_crossing() -> None:
    dst = _ff("b", "$dffe", 3, D=(5,), Q=(6,), EN=(8,))
    p = Port(name="p", direction="input", bits=(8,))
    (c,) = find_crossings(_module(dst, ports=(p,)), port_clock={"p": "clk_a"})
    assert (c.src_port, c.dst_flop.name, c.min_hops) == ("p", "b", 1)


def test_boundary_sourced_enable_crossing() -> None:
    dst = _ff("b", "$dffe", 3, D=(5,), Q=(6,), EN=(9,))
    inst = Cell(name="u", type="sub", connections={"o": (9,)})
    summary = BoundarySummary(
        module="sub",
        clock="clk_a",
        ports={
            "o": PortBoundary(port="o", src_clock="clk_a", synchronised=False, width=1)
        },
    )
    crossings = find_crossings(_module(dst, inst), boundaries={"u": summary})
    (c,) = [c for c in crossings if c.src_boundary is not None]
    assert (c.src_boundary, c.dst_flop.name, c.min_hops) == (("u", "o"), "b", 1)


# --- -through ---------------------------------------------------------------


def _through_covers(module: Module, through_net: str) -> bool:
    spec = sdc_mod.parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        f"set_false_path -through [get_nets {through_net}]\n"
    )
    (c,) = find_crossings(module)
    (rc,) = sdc_objects.resolve(module, spec).constraints
    return sdc_objects._ThroughWalker(module).all_paths_through(c, rc)


def test_through_walker_sees_the_enable_pin() -> None:
    dst = _ff("b", "$dffe", 3, D=(5,), Q=(6,), EN=(7,))
    module = _module(dst)
    module.netnames["en_buf"] = netlist.Netname(name="en_buf", bits=(7,))
    module.netnames["d_b"] = netlist.Netname(name="d_b", bits=(5,))
    # Every path to b's EN passes through en_buf.
    assert _through_covers(module, "en_buf")
    # d_b is not on the enable path: the crossing reaches b around it.
    assert not _through_covers(module, "d_b")
