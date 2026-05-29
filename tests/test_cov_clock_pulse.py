"""Coverage tests for ``clock_network.py`` and ``pulse.py``.

Two structural surfaces sit downstream of the rule pack:

* :func:`rtl_buddy_cdc.clock_network.find_clock_network_crossings` mirrors
  CDC-010's clock-network walk but emits one record per ``(src_flop,
  dst_flop)`` pair so a renderer can draw the edge CDC-010 only reports
  per control triple.
* :mod:`rtl_buddy_cdc.pulse` classifies a flop's ``D``-pin comb cone for
  CDC-009 (``classify_d_pin_shape`` — the edge-detector ``A & ~A_d``
  pulse) and CDC-013 (``classify_toggle_d_pin`` — the ``mux(Q, ~Q, en)``
  toggle).

The committed Yosys-JSON fixtures drive the happy paths
(``netlist.load`` reads JSON, no yosys binary). The many guard / skip
branches in the helpers are exercised by hand-built
:class:`~rtl_buddy_cdc.netlist.Module` objects so each ``continue`` /
early ``return`` is hit deterministically and asserted on by behaviour
(the returned crossing list, the classifier verdict), not coverage
theater.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.clock_network import (
    ClockNetworkCrossing,
    _bit_drivers_with_idx,
    _cell_clock_input_domains,
    _cell_to_downstream_flops,
    _control_kind_for,
    find_clock_network_crossings,
)
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.pulse import classify_d_pin_shape, classify_toggle_d_pin
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX = Path(__file__).parent / "fixtures"


def _module(cells: dict[str, Cell], ports: dict[str, Port] | None = None) -> Module:
    """Tiny synthetic module so helper-level branches are reachable
    without round-tripping through yosys."""
    return Module(name="syn", ports=ports or {}, cells=cells, netnames={})


def _dff(name: str, clk: int, q: int, d: int = 7) -> Cell:
    return Cell(
        name=name, type="$dff", connections={"CLK": (clk,), "D": (d,), "Q": (q,)}
    )


# --------------------------------------------------------------------------- #
# clock_network.find_clock_network_crossings — fixture-driven happy paths
# --------------------------------------------------------------------------- #


def test_async_clock_mux_surfaces_one_mux_select_edge() -> None:
    """``bad_async_clock_mux``: the ``$mux`` whose ``S`` is driven by a
    foreign-domain flop yields exactly one clock-network crossing,
    classified as a mux-select edge from ck1 into ck0."""
    name = "bad_async_clock_mux"
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")

    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert len(crossings) == 1
    c = crossings[0]
    assert isinstance(c, ClockNetworkCrossing)
    assert c.src_clock == "ck1"
    assert c.dst_clock == "ck0"
    assert c.control_pin == "S"
    assert c.control_cell_type == "$mux"
    assert c.control_kind == "mux-select"
    # src drives the select; dst is clocked by the gated output.
    assert c.src_flop.cell.connections.get("Q", ()) == module.netnames["sel_q"].bits
    assert c.dst_flop.cell.connections.get("Q", ()) == module.netnames["q_out"].bits


def test_techmap_mux_select_uses_gate_level_mux_cell() -> None:
    """``bad_techmap_async_clock_mux``: the simplemap'd ``$_MUX_`` is
    still recognised as a clock-network select cell (explicit-map path
    for the gate-level mux family)."""
    name = "bad_techmap_async_clock_mux"
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")

    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert len(crossings) == 1
    c = crossings[0]
    assert c.control_cell_type == "$_MUX_"
    assert c.control_pin == "S"
    assert c.control_kind == "mux-select"
    assert c.src_clock == "ck1"
    assert c.dst_clock == "ck0"


def test_raw_and_clock_gates_surface_no_clock_network_crossing() -> None:
    """``bad_clock_gating`` / ``bad_clock_gating_async_enable``: a bare
    ``$and`` clock gate has no named control pin, so the walk finds the
    cell on the clock network but ``_control_pins_for`` returns empty —
    no crossing is emitted (mirrors the CDC-010 gap pinned in #177)."""
    for name in ("bad_clock_gating", "bad_clock_gating_async_enable"):
        module = netlist.load(FIX / name / f"{name}.json")
        spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")
        crossings = find_clock_network_crossings(
            module, spec, clock_for_port=spec.clock_for_port
        )
        assert crossings == [], (
            f"{name}: a raw-$and clock gate carries no recognised control "
            f"pin, so no clock-network crossing should surface; got {crossings!r}"
        )


def test_clock_as_data_has_no_clock_network_crossing() -> None:
    """``bad_clock_as_data`` is a CDC-008 shape (clock on a data pin),
    not a control-pin glitch — the clock-network surface stays empty."""
    name = "bad_clock_as_data"
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")
    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert crossings == []


# --------------------------------------------------------------------------- #
# clock_network — synthetic gate-enable / async / domain branches
# --------------------------------------------------------------------------- #


def _gate_enable_module() -> Module:
    """``F_src`` (clkA) drives the EN of a ``$dffe`` ICG whose clock
    input is ``F_clkgen`` (clkB); the ICG's Q clocks ``F_dst``. Both
    domains trace to distinct top-level clock ports."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkB": Port("clkB", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_src": _dff("F_src", clk=1, q=2),
        "F_clkgen": _dff("F_clkgen", clk=3, q=4),
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    return _module(cells, ports)


def test_gate_enable_crossing_without_clock_spec() -> None:
    """No clock_spec / no clock_for_port: domains are the raw clock-port
    names and ``_async`` falls back to plain inequality. The ``$dffe``
    EN driven by an async-domain flop produces one gate-enable
    crossing."""
    module = _gate_enable_module()
    crossings = find_clock_network_crossings(module)  # clock_spec=None
    assert len(crossings) == 1
    c = crossings[0]
    assert c.control_pin == "EN"
    assert c.control_kind == "gate-enable"
    assert c.control_cell == "icg"
    assert c.control_cell_type == "$dffe"
    assert c.src_flop.cell.name == "F_src"
    assert c.dst_flop.cell.name == "F_dst"
    assert {c.src_clock, c.dst_clock} == {"clkA", "clkB"}


def test_gate_enable_crossing_with_spec_via_port_clock_input() -> None:
    """clock_spec supplied and the ICG's clock input comes straight from
    a top-level clock port: ``_cell_clock_input_domains`` resolves it via
    ``ClockSpec.clock_for_port`` and ``are_async`` confirms the pair is
    async (per ``set_clock_groups``)."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkB": Port("clkB", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_src": _dff("F_src", clk=1, q=2),
        # ICG clock input is the clkB port directly (bit 3).
        "icg": Cell("icg", "$dffe", {"CLK": (3,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    spec = sdc_mod.parse(
        "create_clock -name clkA -period 10 [get_ports clkA]\n"
        "create_clock -name clkB -period 10 [get_ports clkB]\n"
        "set_clock_groups -asynchronous -group {clkA} -group {clkB}\n"
    )
    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert len(crossings) == 1
    c = crossings[0]
    assert c.src_clock == "clkA"
    assert c.dst_clock == "clkB"
    assert c.control_kind == "gate-enable"


def test_no_crossing_when_control_flop_shares_cell_clock_domain() -> None:
    """If the control flop sits in the *same* domain as the cell's
    clock-input domain, the rule (and this surface) must stay silent —
    driving an enable with a same-domain flop is the safe case
    (``src_clk in cell_clock_domains``)."""
    ports = {"clkB": Port("clkB", "input", (3,)), "din": Port("din", "input", (7,))}
    cells = {
        # ICG clock input traces to F_clkgen.Q (clkB domain).
        "F_clkgen": _dff("F_clkgen", clk=3, q=4),
        # Control flop is also clkB -> same domain as the cell's clock input.
        "F_src": _dff("F_src", clk=3, q=2),
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    assert find_clock_network_crossings(module) == []


def test_unclassifiable_cell_clock_domain_stays_silent() -> None:
    """End-to-end: the control pin has a valid source flop, but the
    cell's own clock input is a dangling net — ``cell_clock_domains``
    comes back empty, so the walk stays silent (``not cell_clock_domains``
    guard) rather than firing blind."""
    ports = {"clkA": Port("clkA", "input", (1,)), "din": Port("din", "input", (7,))}
    cells = {
        "F_src": _dff("F_src", clk=1, q=2),
        # ICG clock input (bit 99) is dangling -> domain unclassifiable.
        "icg": Cell("icg", "$dffe", {"CLK": (99,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    assert find_clock_network_crossings(module) == []


def test_cell_clock_input_depth_limit_gives_up() -> None:
    """A long combinational chain (>12 hops) between the clock-source
    flop's Q and the cell's clock input exceeds ``max_depth``; the walk
    gives up and classifies no domain."""
    cells = {
        "F_clkgen": _dff("F_clkgen", clk=3, q=100),
        "F_src": _dff("F_src", clk=1, q=2),
    }
    prev = 100
    for i in range(14):  # 14 comb hops, beyond the depth-12 budget
        out = 200 + i
        cells[f"b{i}"] = Cell(
            f"b{i}", "$and", {"A": (prev,), "B": (prev,), "Y": (out,)}
        )
        prev = out
    cells["icg"] = Cell(
        "icg", "$dffe", {"CLK": (prev,), "EN": (2,), "D": (7,), "Q": (5,)}
    )
    module = _module(
        cells,
        {"clkA": Port("clkA", "input", (1,)), "clkB": Port("clkB", "input", (3,))},
    )
    drivers = _bit_drivers_with_idx(module)
    domains = _cell_clock_input_domains(
        module,
        module.cells["icg"],
        drivers,
        {"F_src": "clkA", "F_clkgen": "clkB"},
        None,
        frozenset({"EN"}),
    )
    assert domains == set()


def test_constant_bits_on_clock_network_are_ignored() -> None:
    """Constant bits sprinkled through the clock network (a const on a
    gate input, a const on the cell's data pin, a const lane in a wide
    output) are skipped by the int-only filters; the real flop-Q clock
    source still classifies and the crossing still surfaces."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkB": Port("clkB", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_src": _dff("F_src", clk=1, q=2),
        "F_clkgen": _dff("F_clkgen", clk=3, q=30),
        # comb gate with a constant operand feeding the ICG clock input.
        "buf": Cell("buf", "$and", {"A": (30,), "B": ("0",), "Y": (4,)}),
        # ICG data pin is a constant (non-control input that is non-int).
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": ("1",), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
        # a wide output with one constant lane exercises the driver-map filter.
        "wide": Cell("wide", "$add", {"A": (7,), "B": (7,), "Y": (8, "0")}),
    }
    module = _module(cells, ports)
    crossings = find_clock_network_crossings(module)
    assert len(crossings) == 1
    assert {crossings[0].src_clock, crossings[0].dst_clock} == {"clkA", "clkB"}
    assert crossings[0].control_kind == "gate-enable"


def test_cell_clock_input_q_with_unknown_domain_is_skipped() -> None:
    """When a non-control input traces to a flop Q whose own domain is
    None (untraceable clock), nothing is collected from that branch."""
    cells = {
        "F_clkgen": Cell("F_clkgen", "$dff", {"CLK": (4,), "D": (7,), "Q": (40,)}),
        "icg": Cell("icg", "$dffe", {"CLK": (40,), "EN": (2,), "D": (7,), "Q": (5,)}),
    }
    module = _module(cells)
    drivers = _bit_drivers_with_idx(module)
    domains = _cell_clock_input_domains(
        module,
        module.cells["icg"],
        drivers,
        {"F_clkgen": None},  # the clock-source flop has no known domain
        None,
        frozenset({"EN"}),
    )
    assert domains == set()


def test_cell_clock_input_with_dangling_input_classifies_nothing() -> None:
    """A cell whose non-control input bit has neither a driver nor a
    top-level port (a dangling net) contributes no domain even with a
    clock_spec present — the ``port_of_bit is None`` branch."""
    ports = {"clkA": Port("clkA", "input", (1,)), "din": Port("din", "input", (7,))}
    cells = {
        "F_src": _dff("F_src", clk=1, q=2),
        # CLK bit 99 is dangling: no driver, no owning port.
        "icg": Cell("icg", "$dffe", {"CLK": (99,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    spec = sdc_mod.parse("create_clock -name clkA -period 10 [get_ports clkA]\n")
    drivers = _bit_drivers_with_idx(module)
    domains = _cell_clock_input_domains(
        module, module.cells["icg"], drivers, {"F_src": "clkA"}, spec, frozenset({"EN"})
    )
    assert domains == set()


def test_no_crossing_when_async_to_only_one_of_two_clock_inputs() -> None:
    """A clock mux selecting between two gated legs (clkA, clkB). The
    select flop is in clkC, declared async to clkA but synchronous to
    clkB. Because the source is *not* async to every clock-input domain,
    no crossing is emitted (the all-async guard)."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkB": Port("clkB", "input", (11,)),
        "clkC": Port("clkC", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_clkA": _dff("F_clkA", clk=1, q=20),
        "F_clkB": _dff("F_clkB", clk=11, q=21),
        "F_src": _dff("F_src", clk=3, q=2),
        "cmux": Cell("cmux", "$mux", {"A": (20,), "B": (21,), "S": (2,), "Y": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    spec = sdc_mod.parse(
        "create_clock -name clkA -period 10 [get_ports clkA]\n"
        "create_clock -name clkB -period 10 [get_ports clkB]\n"
        "create_clock -name clkC -period 10 [get_ports clkC]\n"
        "set_clock_groups -asynchronous -group {clkA} -group {clkB clkC}\n"
    )
    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert crossings == []


def test_fires_when_select_async_to_every_clock_input() -> None:
    """Same mux as above, but now clkC is async to both clkA and clkB.
    The all-async guard passes; exactly one crossing surfaces (the
    deterministic representative dst_clock is the first sorted member)."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkB": Port("clkB", "input", (11,)),
        "clkC": Port("clkC", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_clkA": _dff("F_clkA", clk=1, q=20),
        "F_clkB": _dff("F_clkB", clk=11, q=21),
        "F_src": _dff("F_src", clk=3, q=2),
        "cmux": Cell("cmux", "$mux", {"A": (20,), "B": (21,), "S": (2,), "Y": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    spec = sdc_mod.parse(
        "create_clock -name clkA -period 10 [get_ports clkA]\n"
        "create_clock -name clkB -period 10 [get_ports clkB]\n"
        "create_clock -name clkC -period 10 [get_ports clkC]\n"
        "set_clock_groups -asynchronous -group {clkA} -group {clkB} -group {clkC}\n"
    )
    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert len(crossings) == 1
    c = crossings[0]
    assert c.src_clock == "clkC"
    # sorted(["clkA","clkB"])[0] is the stable representative.
    assert c.dst_clock == "clkA"
    assert c.control_kind == "mux-select"


def test_control_pin_driven_by_top_level_port_yields_no_crossing() -> None:
    """When the control pin's fanin has no source flop (driven by a
    top-level port / constant), ``_backward_flop_fanin`` is empty and the
    cell is skipped — same posture CDC-010 takes."""
    ports = {
        "clkB": Port("clkB", "input", (3,)),
        "sel": Port("sel", "input", (2,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_clkgen": _dff("F_clkgen", clk=3, q=4),
        # EN is the top-level 'sel' port (bit 2), not a flop Q.
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    assert find_clock_network_crossings(module) == []


def test_no_clock_network_cells_returns_empty() -> None:
    """A design with no clock-distribution logic (every flop clocked
    straight from a port) has no clock-network cells: early-out empty."""
    ports = {"clk": Port("clk", "input", (1,)), "din": Port("din", "input", (7,))}
    module = _module({"F": _dff("F", clk=1, q=2)}, ports)
    assert find_clock_network_crossings(module) == []


def test_clock_input_traced_through_comb_cell_to_flop_q() -> None:
    """The ICG's clock input reaches the source flop's Q through a
    combinational buffer/gate, exercising the multi-hop comb traversal
    in ``_cell_clock_input_domains`` (not a direct Q driver)."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkB": Port("clkB", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_src": _dff("F_src", clk=1, q=2),
        "F_clkgen": _dff("F_clkgen", clk=3, q=30),
        # comb cell between the clkB flop's Q and the ICG clock input.
        "buf": Cell("buf", "$and", {"A": (30,), "B": (30,), "Y": (4,)}),
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    crossings = find_clock_network_crossings(module)
    assert len(crossings) == 1
    assert {crossings[0].src_clock, crossings[0].dst_clock} == {"clkA", "clkB"}


def test_spec_without_clock_for_port_keeps_raw_names() -> None:
    """clock_spec supplied but ``clock_for_port`` left None: ``_resolve``
    returns the raw name and ``are_async`` is consulted on it. The async
    pair still surfaces one crossing."""
    module = _gate_enable_module()
    spec = sdc_mod.parse(
        "create_clock -name clkA -period 10 [get_ports clkA]\n"
        "create_clock -name clkB -period 10 [get_ports clkB]\n"
        "set_clock_groups -asynchronous -group {clkA} -group {clkB}\n"
    )
    crossings = find_clock_network_crossings(module, spec)  # no clock_for_port
    assert len(crossings) == 1
    assert {crossings[0].src_clock, crossings[0].dst_clock} == {"clkA", "clkB"}


def test_empty_control_pin_connection_yields_no_crossing() -> None:
    """A ``$mux`` whose ``S`` connection is the empty tuple has no
    control bits to walk; the cell is skipped."""
    ports = {"clkB": Port("clkB", "input", (3,)), "din": Port("din", "input", (7,))}
    cells = {
        "F_clkgen": _dff("F_clkgen", clk=3, q=4),
        "cmux": Cell("cmux", "$mux", {"A": (4,), "B": (4,), "S": (), "Y": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    assert find_clock_network_crossings(module) == []


def test_untraceable_control_flop_clock_is_skipped() -> None:
    """The control flop's CLK traces to a constant-fed comb cell, so its
    domain is None — the per-source ``src_clk is None`` guard skips it."""
    ports = {"clkB": Port("clkB", "input", (3,)), "din": Port("din", "input", (7,))}
    cells = {
        # F_src.CLK comes off a constant-driven gate -> no clock root.
        "gen": Cell("gen", "$and", {"A": ("1",), "B": ("1",), "Y": (50,)}),
        "F_src": _dff("F_src", clk=50, q=2),
        "F_clkgen": _dff("F_clkgen", clk=3, q=4),
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    assert find_clock_network_crossings(module) == []


def test_multi_select_mux_dedups_to_one_crossing() -> None:
    """A ``$_MUX4_`` carries selects on both ``S`` and ``T``. With both
    driven by the same source flop reaching the same dst flop, the
    second control port hits the ``seen`` set and only one crossing is
    emitted."""
    ports = {
        "clkA": Port("clkA", "input", (1,)),
        "clkC": Port("clkC", "input", (3,)),
        "din": Port("din", "input", (7,)),
    }
    cells = {
        "F_clkA": _dff("F_clkA", clk=1, q=20),
        "F_src": _dff("F_src", clk=3, q=2),
        "m4": Cell(
            "m4",
            "$_MUX4_",
            {
                "A": (20,),
                "B": (20,),
                "C": (20,),
                "D": (20,),
                "S": (2,),
                "T": (2,),
                "Y": (5,),
            },
        ),
        "F_dst": _dff("F_dst", clk=5, q=6),
    }
    module = _module(cells, ports)
    spec = sdc_mod.parse(
        "create_clock -name clkA -period 10 [get_ports clkA]\n"
        "create_clock -name clkC -period 10 [get_ports clkC]\n"
        "set_clock_groups -asynchronous -group {clkA} -group {clkC}\n"
    )
    crossings = find_clock_network_crossings(
        module, spec, clock_for_port=spec.clock_for_port
    )
    assert len(crossings) == 1
    assert crossings[0].src_clock == "clkC"
    assert crossings[0].dst_clock == "clkA"


# --------------------------------------------------------------------------- #
# clock_network — helper-level units
# --------------------------------------------------------------------------- #


def test_cell_to_downstream_flops_dedups_shared_gated_clock() -> None:
    """Two flops sharing one gated-clock net both map to the host ICG;
    the bit-revisit guard keeps the walk terminating."""
    ports = {"clkB": Port("clkB", "input", (3,)), "din": Port("din", "input", (7,))}
    cells = {
        "F_src": _dff("F_src", clk=3, q=2),
        "icg": Cell("icg", "$dffe", {"CLK": (3,), "EN": (2,), "D": (7,), "Q": (5,)}),
        "F_dst1": _dff("F_dst1", clk=5, q=6),
        "F_dst2": _dff("F_dst2", clk=5, q=8),
    }
    module = _module(cells, ports)
    mapping = _cell_to_downstream_flops(module, _bit_drivers_with_idx(module))
    assert mapping["icg"] == {"F_dst1", "F_dst2"}


def test_cell_to_downstream_flops_skips_non_int_clk() -> None:
    """A flop whose CLK is a constant (non-int bit) is skipped — no
    downstream mapping is produced for it."""
    module = _module({"F": Cell("F", "$dff", {"CLK": ("0",), "D": (7,), "Q": (9,)})})
    assert _cell_to_downstream_flops(module, _bit_drivers_with_idx(module)) == {}


def test_cell_clock_input_domains_resolves_flop_q_source() -> None:
    """When a cell's non-control input traces to a flop's Q, that flop's
    domain is collected (the Q-output branch)."""
    cells = {
        "F_clkgen": _dff("F_clkgen", clk=3, q=4),
        "icg": Cell("icg", "$dffe", {"CLK": (4,), "EN": (2,), "D": (7,), "Q": (5,)}),
    }
    module = _module(cells, {"clkB": Port("clkB", "input", (3,))})
    drivers = _bit_drivers_with_idx(module)
    domains = _cell_clock_input_domains(
        module,
        module.cells["icg"],
        drivers,
        {"F_clkgen": "clkB"},
        None,
        frozenset({"EN"}),
    )
    assert domains == {"clkB"}


def test_cell_clock_input_domains_empty_without_spec_on_port_source() -> None:
    """With clock_spec None, a clock input that traces only to a
    top-level port (no driving flop) cannot be classified — the domain
    set stays empty (the false-negative-biased posture)."""
    cells = {
        "icg": Cell("icg", "$dffe", {"CLK": (3,), "EN": (2,), "D": (7,), "Q": (5,)}),
    }
    module = _module(cells, {"clkB": Port("clkB", "input", (3,))})
    drivers = _bit_drivers_with_idx(module)
    domains = _cell_clock_input_domains(
        module, module.cells["icg"], drivers, {}, None, frozenset({"EN"})
    )
    assert domains == set()


def test_control_kind_classifier() -> None:
    """``_control_kind_for``: mux families are mux-select, enable-style
    pins are gate-enable, and the catch-all falls back to mux-select."""
    assert _control_kind_for("S", "$mux") == "mux-select"
    assert _control_kind_for("S", "$_MUX_") == "mux-select"
    assert _control_kind_for("EN", "$dffe") == "gate-enable"
    assert _control_kind_for("ce", "ICG_CELL") == "gate-enable"  # case-insensitive
    assert _control_kind_for("GATE", "SOME_ICG") == "gate-enable"
    # Unknown cell type + non-enable pin name -> mux-select fallback.
    assert _control_kind_for("FOO", "$weird") == "mux-select"


# --------------------------------------------------------------------------- #
# pulse.classify_d_pin_shape — CDC-009 edge-detector classifier
# --------------------------------------------------------------------------- #


def _edge_detector_module(*, delay_d: int = 2, not_type: str = "$not") -> Module:
    """D = F1.Q & ~F2.Q where F2 is the 1-cycle delay of F1 (F2.D ==
    F1.Q). ``delay_d`` lets a test break the F2.D == F1.Q invariant."""
    cells = {
        "F1": _dff("F1", clk=1, q=2),
        "F2": Cell("F2", "$dff", {"CLK": (1,), "D": (delay_d,), "Q": (3,)}),
        "inv": Cell("inv", not_type, {"A": (3,), "Y": (4,)}),
        "and": Cell("and", "$and", {"A": (2,), "B": (4,), "Y": (10,)}),
    }
    return _module(cells)


def _drivers(module: Module) -> dict:
    return _bit_drivers_with_idx(module)


def test_pulse_shape_matches_edge_detector() -> None:
    """The textbook ``A & ~A_d`` edge-detector cone classifies as a
    pulse."""
    module = _edge_detector_module()
    assert (
        classify_d_pin_shape(
            10, "clk", module, _drivers(module), {"F1": "clk", "F2": "clk"}
        )
        == "pulse"
    )


def test_pulse_shape_non_int_d_bit_is_other() -> None:
    """A constant D bit can't be a pulse cone."""
    module = _edge_detector_module()
    assert (
        classify_d_pin_shape(
            "0", "clk", module, _drivers(module), {"F1": "clk", "F2": "clk"}
        )
        == "other"
    )


def test_pulse_shape_multibit_and_input_is_other() -> None:
    """The edge detector AND must have single-bit inputs; a 2-bit A pin
    disqualifies it."""
    cells = {
        "F1": _dff("F1", clk=1, q=2),
        "and": Cell("and", "$and", {"A": (2, 3), "B": (4,), "Y": (10,)}),
    }
    module = _module(cells)
    assert classify_d_pin_shape(10, "clk", module, _drivers(module), {}) == "other"


def test_pulse_shape_constant_and_operand_is_other() -> None:
    """A constant on the AND's A pin (non-int) can't be a flop Q."""
    cells = {"and": Cell("and", "$and", {"A": ("1",), "B": (4,), "Y": (10,)})}
    module = _module(cells)
    assert classify_d_pin_shape(10, "clk", module, _drivers(module), {}) == "other"


def test_pulse_shape_inverted_operand_not_a_not_cell_is_other() -> None:
    """If the supposedly-inverted operand isn't a NOT cell, the pair
    doesn't match the edge detector."""
    module = _edge_detector_module(not_type="$and")
    assert (
        classify_d_pin_shape(
            10, "clk", module, _drivers(module), {"F1": "clk", "F2": "clk"}
        )
        == "other"
    )


def test_pulse_shape_delay_flop_d_mismatch_is_other() -> None:
    """F2 must be F1's 1-cycle delay (F2.D == F1.Q). Break that and the
    cone is no longer an edge detector."""
    module = _edge_detector_module(delay_d=99)
    assert (
        classify_d_pin_shape(
            10, "clk", module, _drivers(module), {"F1": "clk", "F2": "clk"}
        )
        == "other"
    )


def test_pulse_shape_wrong_domain_source_flop_is_other() -> None:
    """The source flops must be in ``src_clock``'s domain; tag them with
    a different clock and the classifier bails (the domain mismatch
    branch of ``_src_flop_d_pin``)."""
    module = _edge_detector_module()
    assert (
        classify_d_pin_shape(
            10, "src", module, _drivers(module), {"F1": "other", "F2": "other"}
        )
        == "other"
    )


def test_pulse_shape_d_bit_without_driver_is_other() -> None:
    """``d_bit`` is an int but nothing drives it: ``_matches_edge_detector``
    bails at the no-driver guard."""
    module = _module({})
    assert classify_d_pin_shape(10, "clk", module, _drivers(module), {}) == "other"


def test_pulse_shape_d_bit_from_non_and_cell_is_other() -> None:
    """``d_bit`` is the Y of a non-AND cell: the cell-type guard rejects
    it."""
    cells = {"or": Cell("or", "$or", {"A": (2,), "B": (4,), "Y": (10,)})}
    module = _module(cells)
    assert classify_d_pin_shape(10, "clk", module, _drivers(module), {}) == "other"


def test_pulse_shape_inverted_operand_without_driver_is_other() -> None:
    """The direct operand is a valid src flop Q, but the would-be
    inverted operand has no driver — the pair check fails."""
    cells = {
        "F1": _dff("F1", clk=1, q=2),
        # B (bit 4) is undriven; A=F1.Q.
        "and": Cell("and", "$and", {"A": (2,), "B": (4,), "Y": (10,)}),
    }
    module = _module(cells)
    assert (
        classify_d_pin_shape(10, "clk", module, _drivers(module), {"F1": "clk"})
        == "other"
    )


def test_pulse_shape_not_cell_multibit_input_is_other() -> None:
    """The NOT cell in the inverted leg must have a single-bit input."""
    cells = {
        "F1": _dff("F1", clk=1, q=2),
        "F2": Cell("F2", "$dff", {"CLK": (1,), "D": (2,), "Q": (3,)}),
        "inv": Cell("inv", "$not", {"A": (3, 99), "Y": (4,)}),
        "and": Cell("and", "$and", {"A": (2,), "B": (4,), "Y": (10,)}),
    }
    module = _module(cells)
    assert (
        classify_d_pin_shape(
            10, "clk", module, _drivers(module), {"F1": "clk", "F2": "clk"}
        )
        == "other"
    )


def test_pulse_shape_delay_leg_not_a_flop_is_other() -> None:
    """The inverted leg's NOT input must be a flop Q (the delay flop);
    here it is a comb output, so ``_src_flop_d_pin`` returns None."""
    cells = {
        "F1": _dff("F1", clk=1, q=2),
        "combd": Cell("combd", "$and", {"A": (7,), "B": (7,), "Y": (40,)}),
        "inv": Cell("inv", "$not", {"A": (40,), "Y": (4,)}),
        "and": Cell("and", "$and", {"A": (2,), "B": (4,), "Y": (10,)}),
    }
    module = _module(cells)
    assert (
        classify_d_pin_shape(10, "clk", module, _drivers(module), {"F1": "clk"})
        == "other"
    )


def test_pulse_shape_direct_operand_from_non_flop_q_is_other() -> None:
    """The direct operand traces to a ``Q`` port of a non-flop cell;
    ``_src_flop_d_pin`` rejects it at the ``is_ff_cell`` guard."""
    cells = {
        "weird": Cell("weird", "$lut", {"Q": (2,)}),
        "inv": Cell("inv", "$not", {"A": (3,), "Y": (4,)}),
        "and": Cell("and", "$and", {"A": (2,), "B": (4,), "Y": (10,)}),
    }
    module = _module(cells)
    assert classify_d_pin_shape(10, "clk", module, _drivers(module), {}) == "other"


def test_pulse_shape_source_flop_multibit_d_is_other() -> None:
    """A single-bit flop is required; a 2-bit D pin disqualifies the
    direct operand's source flop."""
    cells = {
        "F1": Cell("F1", "$dff", {"CLK": (1,), "D": (7, 8), "Q": (2,)}),
        "inv": Cell("inv", "$not", {"A": (3,), "Y": (4,)}),
        "and": Cell("and", "$and", {"A": (2,), "B": (4,), "Y": (10,)}),
    }
    module = _module(cells)
    assert (
        classify_d_pin_shape(10, "clk", module, _drivers(module), {"F1": "clk"})
        == "other"
    )


# --------------------------------------------------------------------------- #
# pulse.classify_toggle_d_pin — CDC-013 toggle classifier
# --------------------------------------------------------------------------- #


def _toggle_module(
    *, a: int = 2, b: int = 3, not_a_src: int = 2, mux_type: str = "$mux"
):
    """D = mux(A=a, B=b, S=en); ``b`` is ``~not_a_src`` via a NOT cell.
    Defaults form the canonical toggle ``mux(Q, ~Q, en)`` with Q=bit 2."""
    cells = {
        "inv": Cell("inv", "$not", {"A": (not_a_src,), "Y": (3,)}),
        "mux": Cell("mux", mux_type, {"A": (a,), "B": (b,), "S": (9,), "Y": (10,)}),
    }
    return _module(cells)


def test_toggle_shape_matches_canonical_mux() -> None:
    """``mux(A=Q, B=~Q, S=en)`` classifies as a toggle."""
    module = _toggle_module()
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "toggle"


def test_toggle_shape_matches_mirror_ordering() -> None:
    """``mux(A=~Q, B=Q, S=en)`` (operands swapped) is still a toggle."""
    module = _toggle_module(a=3, b=2)
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "toggle"


def test_toggle_shape_non_int_inputs_are_other() -> None:
    module = _toggle_module()
    assert classify_toggle_d_pin("0", 2, module, _drivers(module)) == "other"
    assert classify_toggle_d_pin(10, "1", module, _drivers(module)) == "other"


def test_toggle_shape_no_driver_is_other() -> None:
    module = _toggle_module()
    assert classify_toggle_d_pin(777, 2, module, _drivers(module)) == "other"


def test_toggle_shape_non_mux_cell_is_other() -> None:
    """Only ``$mux`` is the toggle shape; an adder is not."""
    module = _toggle_module(mux_type="$add")
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "other"


def test_toggle_shape_multibit_mux_input_is_other() -> None:
    cells = {
        "mux": Cell("mux", "$mux", {"A": (2, 99), "B": (3,), "S": (9,), "Y": (10,)})
    }
    module = _module(cells)
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "other"


def test_toggle_shape_constant_mux_operand_is_other() -> None:
    cells = {
        "mux": Cell("mux", "$mux", {"A": ("1",), "B": (3,), "S": (9,), "Y": (10,)})
    }
    module = _module(cells)
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "other"


def test_toggle_shape_neither_operand_is_src_q_is_other() -> None:
    """Neither mux data input equals the source flop's Q (src_q=5 here),
    so the loop never enters the inverted-operand check."""
    module = _toggle_module()
    assert classify_toggle_d_pin(10, 5, module, _drivers(module)) == "other"


def test_toggle_shape_inverted_operand_without_driver_is_other() -> None:
    """A=Q matches but B has no driver — can't be ~Q."""
    cells = {
        "mux": Cell("mux", "$mux", {"A": (2,), "B": (888,), "S": (9,), "Y": (10,)})
    }
    module = _module(cells)
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "other"


def test_toggle_shape_inverted_operand_not_a_not_cell_is_other() -> None:
    cells = {
        "x": Cell("x", "$and", {"A": (2,), "Y": (3,)}),
        "mux": Cell("mux", "$mux", {"A": (2,), "B": (3,), "S": (9,), "Y": (10,)}),
    }
    module = _module(cells)
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "other"


def test_toggle_shape_not_input_not_src_q_is_other() -> None:
    """The NOT cell's input must be the source Q; here it inverts a
    different net, so the inner branch falls through and the loop
    continues without matching."""
    module = _toggle_module(not_a_src=99)
    assert classify_toggle_d_pin(10, 2, module, _drivers(module)) == "other"


# --------------------------------------------------------------------------- #
# End-to-end: CDC-009 firing through the pulse classifier on a fixture
# --------------------------------------------------------------------------- #


def test_cdc_009_fires_on_pulse_width_fixture() -> None:
    """``bad_pulse_width_fast_to_slow``: the rule pack drives
    ``classify_d_pin_shape`` and the single fast→slow pulse crossing
    surfaces as one CDC-009 warning."""
    name = "bad_pulse_width_fast_to_slow"
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)

    violations = run_all_rules(module, crossings, spec)
    cdc_009 = [v for v in violations if v.rule_id == "CDC-009"]
    assert len(cdc_009) == 1
    v = cdc_009[0]
    assert v.severity == "warning"
    assert "pulse-width risk" in v.message
