"""Coverage tests for the small support modules around the analyzer core.

These exercise edge branches in the data-model and plumbing layers that
the end-to-end fixture suite doesn't reach directly:

- ``netlist.load`` error / multi-module / parsing paths (tiny JSON dicts
  written into ``tmp_path`` — no Yosys binary needed),
- ``domain.trace_clock_root`` / ``domain.find_crossings`` structural
  branches (buffer / mux / divider tracing, port-sourced crossings),
  driven from hand-built :class:`Module` objects,
- ``domain_map`` serialization of generated clocks, exclusive groups,
  port-sourced crossings, and clock-network crossings,
- ``flops`` FF-zoo edge cases (malformed CLK, ``Flop.width``),
- ``frontend`` enum dispatch and the ``auto`` resolution line,
- ``frontends.yosys`` guard / error branches, reached by monkeypatching
  ``shutil.which`` / ``subprocess.run`` so no real Yosys is invoked.

Everything here is deterministic and toolchain-free except the single
``auto``-dispatch test, which is gated on pyslang (present in the
coverage CI job).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import domain_map, frontend as frontend_mod, netlist
from rtl_buddy_cdc.clock_network import ClockNetworkCrossing
from rtl_buddy_cdc.domain import (
    Crossing,
    FlopDomain,
    assign_domains,
    find_crossings,
    trace_clock_root,
)
from rtl_buddy_cdc.flops import Flop, find_flops, flop_clk_pin, is_ff_cell
from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.frontends import yosys as yosys_fe
from rtl_buddy_cdc.frontends.yosys import YosysError
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.sdc import UNCONSTRAINED_SENTINEL, Clock, ClockSpec

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None


# --------------------------------------------------------------------------
# netlist.load — error and parsing edge cases (tiny JSON in tmp_path)
# --------------------------------------------------------------------------


def _write_json(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "design.json"
    p.write_text(json.dumps(data))
    return p


def test_load_empty_modules_raises(tmp_path: Path) -> None:
    """A JSON with no ``modules`` key is unusable — load must reject it
    with a message naming the offending file (netlist.py:83)."""
    path = _write_json(tmp_path, {"modules": {}})
    with pytest.raises(ValueError, match="no modules in JSON"):
        netlist.load(path)


def test_load_picks_single_non_dollar_module(tmp_path: Path) -> None:
    """With more than one module, the loader keeps the single user module
    and discards ``$``-prefixed paramod/blackbox stubs (netlist.py:87-93,
    happy multi-module branch)."""
    data = {
        "modules": {
            "$paramod$abc\\stub": {"ports": {}, "cells": {}, "netnames": {}},
            "top": {
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                },
                "cells": {},
                "netnames": {},
            },
        }
    }
    module = netlist.load(_write_json(tmp_path, data))
    assert module.name == "top"
    assert module.ports["clk"].direction == "input"
    assert module.ports["clk"].bits == (2,)


def test_load_ambiguous_multi_module_raises(tmp_path: Path) -> None:
    """Two non-``$`` modules after flatten is ambiguous — the loader
    can't know which is the design top, so it raises (netlist.py:88-92)."""
    data = {
        "modules": {
            "alpha": {"ports": {}, "cells": {}, "netnames": {}},
            "beta": {"ports": {}, "cells": {}, "netnames": {}},
        }
    }
    with pytest.raises(ValueError, match="expected exactly one user module"):
        netlist.load(_write_json(tmp_path, data))


def test_load_parses_cells_and_netnames(tmp_path: Path) -> None:
    """Full port/cell/netname parsing path: parameters, attributes, and
    bit tuples are all carried through (netlist.py cell/netname comps)."""
    data = {
        "modules": {
            "m": {
                "ports": {
                    "a": {"direction": "input", "bits": [2, 3]},
                    "y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "u_and": {
                        "type": "$and",
                        "connections": {"A": [2], "B": [3], "Y": [4]},
                        "parameters": {"WIDTH": "1"},
                        "attributes": {"src": "m.sv:1"},
                    }
                },
                "netnames": {
                    "sig": {"bits": [4], "attributes": {"keep": "1"}},
                },
            }
        }
    }
    module = netlist.load(_write_json(tmp_path, data))
    cell = module.cells["u_and"]
    assert cell.type == "$and"
    assert cell.connections == {"A": (2,), "B": (3,), "Y": (4,)}
    assert cell.parameters == {"WIDTH": "1"}
    assert cell.attributes == {"src": "m.sv:1"}
    nn = module.netnames["sig"]
    assert nn.bits == (4,)
    assert nn.attributes == {"keep": "1"}


def test_module_port_of_bit_const_returns_none() -> None:
    """A constant bit (string ``"0"``) is never owned by a port; the
    lookup short-circuits before scanning ports (netlist.py:58)."""
    port = Port(name="a", direction="input", bits=(2,))
    module = Module(name="m", ports={"a": port}, cells={}, netnames={})
    assert module.port_of_bit("0") is None
    assert module.port_of_bit(2) is port
    assert module.port_of_bit(99) is None


def test_module_cells_of_type_filters() -> None:
    """``cells_of_type`` returns only cells whose type is in the wanted
    set (netlist.py:65-66)."""
    c1 = Cell(name="g0", type="$and", connections={})
    c2 = Cell(name="g1", type="$or", connections={})
    c3 = Cell(name="g2", type="$and", connections={})
    module = Module(
        name="m", ports={}, cells={"g0": c1, "g1": c2, "g2": c3}, netnames={}
    )
    ands = module.cells_of_type(["$and"])
    assert {c.name for c in ands} == {"g0", "g2"}
    assert module.cells_of_type(["$xor"]) == []


# --------------------------------------------------------------------------
# flops — FF zoo edge cases
# --------------------------------------------------------------------------


def test_is_ff_cell_gate_level_prefix_and_negative() -> None:
    """Prefix matching recognises the gate-level explosion variants but
    rejects non-FF cells (flops.py is_ff_cell)."""
    assert is_ff_cell("$dff")
    assert is_ff_cell("$_DFFE_PP0P_")
    assert is_ff_cell("$_SDFFCE_NN1N_")
    assert not is_ff_cell("$and")
    assert not is_ff_cell("$dlatch")


def test_is_latch_cell_recognises_latch_families() -> None:
    """``is_latch_cell`` recognises both the higher-level ``$dlatch`` and
    the gate-level ``$_DLATCH_*`` family, rejecting flops (flops.py:95)."""
    from rtl_buddy_cdc.flops import is_latch_cell

    assert is_latch_cell("$dlatch")
    assert is_latch_cell("$_DLATCH_P_")
    assert is_latch_cell("$_DLATCH_PP0_")
    assert not is_latch_cell("$dff")
    assert not is_latch_cell("$and")


def test_flop_clk_pin_prefers_uppercase_clk_then_c() -> None:
    """``flop_clk_pin`` is family-portable: ``CLK`` for higher-level
    cells, ``C`` for gate-level; ``None`` when neither connects."""
    hi = Cell(name="f0", type="$dff", connections={"CLK": (2,)})
    lo = Cell(name="f1", type="$_DFF_P_", connections={"C": (3,)})
    none = Cell(name="f2", type="$dff", connections={"D": (4,)})
    assert flop_clk_pin(hi) == (2,)
    assert flop_clk_pin(lo) == (3,)
    assert flop_clk_pin(none) is None


def test_find_flops_skips_malformed_clk_width() -> None:
    """A flop whose CLK pin is a multi-bit vector is malformed; the
    analyzer skips it defensively rather than raising (flops.py:141).
    A well-formed single-bit-CLK flop in the same module is kept."""
    bad = Cell(name="f_bad", type="$dff", connections={"CLK": (2, 3), "Q": (5,)})
    good = Cell(
        name="f_good", type="$dff", connections={"CLK": (2,), "D": (6,), "Q": (7,)}
    )
    module = Module(
        name="m", ports={}, cells={"f_bad": bad, "f_good": good}, netnames={}
    )
    flops = find_flops(module)
    assert [f.name for f in flops] == ["f_good"]
    assert flops[0].clk == 2
    assert flops[0].d == (6,)


def test_flop_width_property() -> None:
    """``Flop.width`` is the count of Q bits (flops.py:123)."""
    cell = Cell(name="f", type="$dff", connections={"CLK": (2,), "Q": (5, 6, 7)})
    flop = Flop(cell=cell, clk=2, d=(8, 9, 10), q=(5, 6, 7))
    assert flop.width == 3
    assert flop.name == "f"


# --------------------------------------------------------------------------
# domain.trace_clock_root — structural tracing branches
# --------------------------------------------------------------------------


def _mod(ports: dict, cells: dict, netnames: dict | None = None) -> Module:
    return Module(name="m", ports=ports, cells=cells, netnames=netnames or {})


def test_trace_root_direct_input_port() -> None:
    """The trivial case: the CLK bit is a top-level input port bit, so
    the trace returns the port name immediately."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,))},
        cells={},
    )
    assert trace_clock_root(module, 10) == "clk"


def test_trace_root_through_buffer_and_inverter() -> None:
    """Single-input buffers / inverters are transparent: the trace walks
    A back to the clock port (domain.py:196-199)."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,))},
        cells={
            "inv": Cell("inv", "$_NOT_", {"A": (10,), "Y": (11,)}),
            "buf": Cell("buf", "$buf", {"A": (11,), "Y": (12,)}),
        },
    )
    assert trace_clock_root(module, 12) == "clk"


def test_trace_root_buffer_without_input_returns_none() -> None:
    """A buffer with no ``A`` connection cannot be followed back; the
    trace gives up (domain.py:199, the empty-A return)."""
    module = _mod(
        ports={},
        cells={"buf": Cell("buf", "$buf", {"Y": (12,)})},
    )
    assert trace_clock_root(module, 12) is None


def test_trace_root_clock_gate_one_side_resolves() -> None:
    """A two-input clock gate where only A traces to a clock port returns
    that port; the unresolved enable leg (B) is ignored."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,)), "en": Port("en", "input", (20,))},
        cells={"icg": Cell("icg", "$and", {"A": (10,), "B": (99,), "Y": (30,)})},
    )
    # A resolves to clk; B (bit 99) has no driver and no port → None.
    assert trace_clock_root(module, 30) == "clk"


def test_trace_root_mux_first_side_none_then_second_resolves() -> None:
    """A clock mux explores A then B; when A is unresolvable the trace
    falls through to B (domain.py mux loop, both legs)."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,))},
        # A is a dangling bit (no driver, no port) → None; B is the clock.
        cells={"mux": Cell("mux", "$mux", {"A": (88,), "B": (10,), "Y": (40,)})},
    )
    assert trace_clock_root(module, 40) == "clk"


def test_trace_root_mux_empty_a_falls_through_to_b() -> None:
    """A clock mux with no ``A`` connection skips that leg and resolves
    via ``B`` (domain.py mux loop, empty-input-port branch 225->223)."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,))},
        # A is absent entirely; B carries the clock.
        cells={"mux": Cell("mux", "$mux", {"B": (10,), "Y": (40,)})},
    )
    assert trace_clock_root(module, 40) == "clk"


def test_trace_root_unknown_driver_cell_returns_none() -> None:
    """A driver cell that is neither buffer, gate, mux, nor FF on its Q
    output falls through all clock-network shapes to the final None
    (domain.py:236->241 false branch)."""
    module = _mod(
        ports={},
        # An adder drives bit 40 on Y — not a recognised clock-network
        # cell, so the trace can't continue.
        cells={"add": Cell("add", "$add", {"A": (1,), "B": (2,), "Y": (40,)})},
    )
    assert trace_clock_root(module, 40) is None


def test_trace_root_mux_both_unresolved_returns_none() -> None:
    """When neither mux leg resolves to a clock port the trace returns
    None (domain.py:231)."""
    module = _mod(
        ports={},
        cells={"mux": Cell("mux", "$mux", {"A": (88,), "B": (89,), "Y": (40,)})},
    )
    assert trace_clock_root(module, 40) is None


def test_trace_root_clock_divider_follows_flop_clk() -> None:
    """A clock-divider flop's Q is followed back to that flop's own CLK
    pin, which is the upstream clock root (domain.py divider branch)."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,))},
        cells={
            "div": Cell("div", "$dff", {"CLK": (10,), "D": (51,), "Q": (50,)}),
            # consumer flop clocked off the divided clock (bit 50)
        },
    )
    assert trace_clock_root(module, 50) == "clk"


def test_trace_root_divider_flop_without_clk_returns_none() -> None:
    """A divider flop whose Q is followed but which has no connected CLK
    pin cannot be traced further; the divider branch returns None
    (domain.py:241)."""
    module = _mod(
        ports={},
        # A flop driving bit 50 on Q but with no CLK/C connection at all.
        cells={"div": Cell("div", "$dff", {"D": (51,), "Q": (50,)})},
    )
    assert trace_clock_root(module, 50) is None


def test_find_crossings_port_walk_skips_untraceable_dst() -> None:
    """A typed input port fanning into a flop whose clock is untraceable
    yields no port-sourced crossing (domain.py:462 None-clock skip)."""
    module = _mod(
        ports={"din": Port("din", "input", (10,))},
        cells={
            # dst flop's CLK (bit 88) has no driver and no port → clock None.
            "f_dst": Cell("f_dst", "$dff", {"CLK": (88,), "D": (10,), "Q": (11,)}),
        },
    )
    crossings = find_crossings(module, port_clock={"din": "ck_in"})
    assert [c for c in crossings if c.is_port_sourced] == []


def test_trace_root_non_int_bit_returns_none() -> None:
    """A constant bit ("1") is not a net; the recursive walk bails at the
    isinstance guard (domain.py:174)."""
    module = _mod(ports={}, cells={})
    assert trace_clock_root(module, "1") is None


def test_trace_root_undriven_bit_returns_none() -> None:
    """A net with no driver and no owning port can't be resolved
    (domain.py:189)."""
    module = _mod(ports={}, cells={})
    assert trace_clock_root(module, 7) is None


def test_trace_root_stops_at_generated_pin_clock() -> None:
    """When ``bit_to_clock`` declares a generated clock at an internal
    pin, the walk stops there and returns the generated name rather than
    continuing to the top input port."""
    module = _mod(
        ports={"clk": Port("clk", "input", (10,))},
        cells={"buf": Cell("buf", "$buf", {"A": (10,), "Y": (11,)})},
    )
    # Bit 11 is declared as the generated clock "gen_clk".
    root = trace_clock_root(module, 11, bit_to_clock={11: "gen_clk"})
    assert root == "gen_clk"


# --------------------------------------------------------------------------
# domain.assign_domains / find_crossings — clock_for_port + port crossings
# --------------------------------------------------------------------------


def _two_domain_module() -> Module:
    """A direct flop→flop crossing: f_src on clk_a, f_dst on clk_b, Q(f_src)
    wired straight to D(f_dst)."""
    return _mod(
        ports={
            "clk_a": Port("clk_a", "input", (1,)),
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (10,)),
        },
        cells={
            "f_src": Cell("f_src", "$dff", {"CLK": (1,), "D": (10,), "Q": (20,)}),
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (20,), "Q": (21,)}),
        },
    )


def test_assign_domains_normalizes_via_clock_for_port() -> None:
    """``clock_for_port`` rewrites the raw traced port name to the
    SDC-canonical clock (domain.py assign_domains normalisation)."""
    module = _two_domain_module()
    mapping = {"clk_a": "ck0", "clk_b": "ck1"}
    domains = assign_domains(module, clock_for_port=lambda p: mapping.get(p))
    by_name = {fd.flop.name: fd.clock for fd in domains}
    assert by_name == {"f_src": "ck0", "f_dst": "ck1"}


def test_assign_domains_pin_clocks_via_netname() -> None:
    """``pin_clocks`` flows through ``_build_bit_to_clock``: the SDC pin
    path (``u/clk_out``) is normalised to the Yosys netname form
    (``u.clk_out``), its bits harvested, and the trace stops at that
    generated-clock identity (domain.py:261 bit harvest)."""
    module = _mod(
        ports={"clk": Port("clk", "input", (1,))},
        cells={
            "buf": Cell("buf", "$buf", {"A": (1,), "Y": (5,)}),
            "f": Cell("f", "$dff", {"CLK": (5,), "D": (8,), "Q": (9,)}),
        },
        # Netname carries a constant bit ("0") alongside the real net so
        # the int-guard skip branch is exercised too.
        netnames={"u.clk_out": Netname("u.clk_out", ("0", 5))},
    )
    # Second entry has no matching netname → the missing-netname continue
    # is exercised; it contributes no bit-to-clock mapping.
    domains = assign_domains(
        module,
        pin_clocks={"u/clk_out": "gen_ck", "absent/pin": "nope"},
    )
    by_name = {fd.flop.name: fd.clock for fd in domains}
    assert by_name == {"f": "gen_ck"}


def test_assign_domains_clock_for_port_returns_none_keeps_raw() -> None:
    """When ``clock_for_port`` returns ``None`` (no SDC mapping for the
    traced port), the raw traced name is kept unchanged (domain.py:296
    skip branch)."""
    module = _two_domain_module()
    # Mapping covers clk_a only; clk_b -> None so its raw name survives.
    domains = assign_domains(module, clock_for_port=lambda p: {"clk_a": "ck0"}.get(p))
    by_name = {fd.flop.name: fd.clock for fd in domains}
    assert by_name == {"f_src": "ck0", "f_dst": "clk_b"}


def test_find_crossings_direct_flop_to_flop() -> None:
    """A direct flop→flop wire in distinct domains is one crossing of
    width 1 and min_hops 0."""
    module = _two_domain_module()
    crossings = find_crossings(module)
    assert len(crossings) == 1
    c = crossings[0]
    assert c.src_flop is not None and c.src_flop.name == "f_src"
    assert c.dst_flop.name == "f_dst"
    assert c.src_clock == "clk_a"
    assert c.dst_clock == "clk_b"
    assert c.min_hops == 0
    assert c.width == 1
    assert not c.is_port_sourced
    # Flop-sourced crossings report the source flop's name (domain.py:87).
    assert c.src_name == "f_src"


def test_find_crossings_port_sourced(tmp_path: Path) -> None:
    """With ``port_clock`` supplied, a typed input port that fans into a
    flop in a different domain produces a port-sourced crossing
    (domain.py port-walk branches)."""
    module = _mod(
        ports={
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (10,)),
        },
        cells={
            # din feeds a comb buffer then the flop's D, clocked on clk_b.
            "buf": Cell("buf", "$buf", {"A": (10,), "Y": (11,)}),
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (11,), "Q": (21,)}),
        },
    )
    crossings = find_crossings(module, port_clock={"din": "clk_in"})
    port_cross = [c for c in crossings if c.is_port_sourced]
    assert len(port_cross) == 1
    c = port_cross[0]
    assert c.src_port == "din"
    assert c.src_name == "port din"
    assert c.src_clock == "clk_in"
    assert c.dst_clock == "clk_b"
    # din → buf → f_dst.D is one comb hop.
    assert c.min_hops == 1


def test_find_crossings_skips_untraceable_self_and_same_domain() -> None:
    """The flop walk skips three non-crossings: a source flop whose clock
    is untraceable (domain.py:372), a flop feeding its own D pin
    (self-loop, 390), and a same-domain destination (393). Only the
    genuine cross-domain edge survives."""
    module = _mod(
        ports={
            "clk_a": Port("clk_a", "input", (1,)),
            "clk_b": Port("clk_b", "input", (2,)),
        },
        cells={
            # f_dangle's CLK (bit 77) has no driver and no port → clock
            # untraceable → it's skipped as a crossing source.
            "f_dangle": Cell(
                "f_dangle", "$dff", {"CLK": (77,), "D": (40,), "Q": (40,)}
            ),
            # f_self feeds its own D (Q == D == bit 30): self-loop skip.
            "f_self": Cell("f_self", "$dff", {"CLK": (1,), "D": (30,), "Q": (30,)}),
            # f_same shares clk_a with f_src2; same-domain → no crossing.
            "f_src2": Cell("f_src2", "$dff", {"CLK": (1,), "D": (9,), "Q": (31,)}),
            "f_same": Cell("f_same", "$dff", {"CLK": (1,), "D": (31,), "Q": (32,)}),
            # The one real crossing: f_src2 (clk_a) → f_cross (clk_b).
            "f_cross": Cell("f_cross", "$dff", {"CLK": (2,), "D": (31,), "Q": (33,)}),
        },
    )
    crossings = find_crossings(module)
    flop_cross = [c for c in crossings if not c.is_port_sourced]
    assert len(flop_cross) == 1
    c = flop_cross[0]
    assert c.src_flop is not None and c.src_flop.name == "f_src2"
    assert c.dst_flop.name == "f_cross"
    assert c.src_clock == "clk_a"
    assert c.dst_clock == "clk_b"


def test_find_crossings_port_walk_skips_missing_and_output_ports() -> None:
    """The port walk skips ports that aren't real input ports: an output
    port and a name absent from the module are both ignored
    (domain.py:451 missing/wrong-direction guard)."""
    module = _mod(
        ports={
            "clk_b": Port("clk_b", "input", (2,)),
            "qout": Port("qout", "output", (10,)),
            "din": Port("din", "input", (11,)),
        },
        cells={"f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (11,), "Q": (12,)})},
    )
    # qout is an output (skipped), "ghost" isn't a port at all (skipped),
    # only din yields a port-sourced crossing.
    crossings = find_crossings(
        module,
        port_clock={"qout": "ck_o", "ghost": "ck_g", "din": "ck_in"},
    )
    port_cross = [c for c in crossings if c.is_port_sourced]
    assert [c.src_port for c in port_cross] == ["din"]
    assert port_cross[0].dst_clock == "clk_b"


def test_find_crossings_respects_max_hops() -> None:
    """With ``max_hops=1`` a destination two comb cells deep is never
    reached, so no crossing is reported — the frontier stops at the hop
    limit (domain.py:410 / 482 hop-limit guard)."""
    module = _mod(
        ports={
            "clk_a": Port("clk_a", "input", (1,)),
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (50,)),
        },
        cells={
            "f_src": Cell("f_src", "$dff", {"CLK": (1,), "D": (9,), "Q": (20,)}),
            "b0": Cell("b0", "$buf", {"A": (20,), "Y": (21,)}),
            "b1": Cell("b1", "$buf", {"A": (21,), "Y": (22,)}),
            # dst is 2 comb hops from f_src.Q (20 -> 21 -> 22).
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (22,), "Q": (23,)}),
        },
    )
    # max_hops=1 cannot reach a 2-hop-deep destination.
    assert find_crossings(module, max_hops=1) == []
    # max_hops=2 does.
    deep = find_crossings(module, max_hops=2)
    assert len(deep) == 1
    assert deep[0].min_hops == 2


def test_find_crossings_non_int_output_bit_skipped() -> None:
    """A comb cell whose output vector contains a constant bit ("0")
    propagates only the integer net, skipping the constant — both the
    flop walk (domain.py:424) and the port walk (domain.py:492) take the
    isinstance-skip branch."""
    module = _mod(
        ports={
            "clk_a": Port("clk_a", "input", (1,)),
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (60,)),
        },
        cells={
            "f_src": Cell("f_src", "$dff", {"CLK": (1,), "D": (9,), "Q": (20,)}),
            # Comb cell driving a 2-bit Y where one bit is the constant 0.
            "mix": Cell("mix", "$buf", {"A": (20,), "Y": ("0", 21)}),
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (21,), "Q": (22,)}),
            # Port path: din through a cell with a constant output bit too.
            "pmix": Cell("pmix", "$buf", {"A": (60,), "Y": ("0", 61)}),
            "f_pdst": Cell("f_pdst", "$dff", {"CLK": (2,), "D": (61,), "Q": (62,)}),
        },
    )
    crossings = find_crossings(module, port_clock={"din": "clk_in"})
    flop_cross = [c for c in crossings if not c.is_port_sourced]
    port_cross = [c for c in crossings if c.is_port_sourced]
    assert len(flop_cross) == 1
    assert flop_cross[0].dst_flop.name == "f_dst"
    assert len(port_cross) == 1
    assert port_cross[0].dst_flop.name == "f_pdst"


def test_find_crossings_port_same_domain_no_crossing() -> None:
    """When the typed port's clock equals the destination flop's clock,
    the port-walk emits nothing (domain.py:465 same-domain skip)."""
    module = _mod(
        ports={
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (10,)),
        },
        cells={"f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (10,), "Q": (21,)})},
    )
    # Port typed to the SAME clock domain as the destination flop.
    crossings = find_crossings(module, port_clock={"din": "clk_b"})
    assert [c for c in crossings if c.is_port_sourced] == []


def test_find_crossings_multibit_bus_collapses_to_one() -> None:
    """A 2-bit bus from one source flop to one destination flop collapses
    to a single Crossing of width 2 — the second bit takes the
    bus-merge branch on the same (src, dst) key (domain.py:406-408)."""
    module = _mod(
        ports={
            "clk_a": Port("clk_a", "input", (1,)),
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (10, 11)),
        },
        cells={
            # 2-bit source flop on clk_a, Q = bits {20, 21}.
            "f_src": Cell("f_src", "$dff", {"CLK": (1,), "D": (10, 11), "Q": (20, 21)}),
            # 2-bit destination flop on clk_b, D = {20, 21}.
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (20, 21), "Q": (30, 31)}),
        },
    )
    crossings = find_crossings(module)
    flop_cross = [c for c in crossings if not c.is_port_sourced]
    assert len(flop_cross) == 1
    c = flop_cross[0]
    assert c.width == 2
    assert c.min_hops == 0
    assert c.src_clock == "clk_a"
    assert c.dst_clock == "clk_b"


def test_find_crossings_skips_scopeinfo_transit_cell() -> None:
    """A ``$scopeinfo`` pseudo-cell on a fanout net is skipped as a
    transit node; the real comb cell behind it still propagates the
    crossing (domain.py:416 scopeinfo continue)."""
    module = _mod(
        ports={
            "clk_a": Port("clk_a", "input", (1,)),
            "clk_b": Port("clk_b", "input", (2,)),
        },
        cells={
            "f_src": Cell("f_src", "$dff", {"CLK": (1,), "D": (9,), "Q": (20,)}),
            # $scopeinfo also reads bit 20 but produces nothing usable.
            "scope": Cell("scope", "$scopeinfo", {"A": (20,)}),
            # Real comb buffer carries bit 20 → bit 21.
            "buf": Cell("buf", "$buf", {"A": (20,), "Y": (21,)}),
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (21,), "Q": (22,)}),
        },
    )
    crossings = find_crossings(module)
    flop_cross = [c for c in crossings if not c.is_port_sourced]
    assert len(flop_cross) == 1
    # src → buf → dst is one comb hop (the $scopeinfo dead-ends).
    assert flop_cross[0].min_hops == 1


def test_find_crossings_port_walk_multibit_and_scopeinfo() -> None:
    """The port-sourced walk mirrors the flop walk: a 2-bit typed port
    fanning into a 2-bit destination flop collapses to width 2, and a
    ``$scopeinfo`` transit cell on the port net is skipped
    (domain.py:479-498)."""
    module = _mod(
        ports={
            "clk_b": Port("clk_b", "input", (2,)),
            "din": Port("din", "input", (10, 11)),
        },
        cells={
            "scope": Cell("scope", "$scopeinfo", {"A": (10,)}),
            # 2-bit destination flop in clk_b domain reads din bits directly.
            "f_dst": Cell("f_dst", "$dff", {"CLK": (2,), "D": (10, 11), "Q": (30, 31)}),
        },
    )
    crossings = find_crossings(module, port_clock={"din": "clk_in"})
    port_cross = [c for c in crossings if c.is_port_sourced]
    assert len(port_cross) == 1
    c = port_cross[0]
    assert c.src_port == "din"
    assert c.width == 2
    assert c.min_hops == 0
    assert c.src_clock == "clk_in"
    assert c.dst_clock == "clk_b"


def test_crossing_src_name_unknown() -> None:
    """A degenerate Crossing with neither a source flop nor a source port
    reports ``<unknown>`` (domain.py:90)."""
    dst_cell = Cell("f_dst", "$dff", {"CLK": (2,), "D": (1,), "Q": (3,)})
    dst = Flop(cell=dst_cell, clk=2, d=(1,), q=(3,))
    c = Crossing(
        src_clock="ck0",
        dst_flop=dst,
        dst_clock="ck1",
        min_hops=0,
        width=1,
    )
    assert c.src_name == "<unknown>"


# --------------------------------------------------------------------------
# domain_map serialization — generated clocks, exclusive groups, ports,
# port-sourced + clock-network crossings, leaf-name helpers
# --------------------------------------------------------------------------


def test_build_domain_map_generated_clock_with_ports() -> None:
    """A port-targeted generated clock serializes with its ``ports`` list,
    a real (non-generated) clock under ``clocks``, and an exclusive group
    under ``clock_groups`` (domain_map.py:96-106, 128-129)."""
    spec = ClockSpec()
    spec.clocks["sys"] = Clock(name="sys", period=10.0, ports=("clk",))
    spec.clocks["genck"] = Clock(
        name="genck",
        period=20.0,
        ports=("div_out",),
        master="sys",
        is_generated=True,
    )
    spec.exclusive_groups.append([{"ck0"}, {"ck1"}])
    module = _mod(ports={"clk": Port("clk", "input", (1,))}, cells={})
    dm = domain_map.build_domain_map(module, [], [], spec)

    assert dm["clocks"] == [
        {"name": "sys", "period": 10.0, "source": "create_clock", "ports": ["clk"]}
    ]
    gen = dm["generated_clocks"]
    assert gen == [
        {"name": "genck", "master": "sys", "period": 20.0, "ports": ["div_out"]}
    ]
    grp = dm["clock_groups"]
    assert grp == [{"kind": "exclusive", "members": [["ck0"], ["ck1"]]}]


def test_build_domain_map_async_group_and_flop_crossing_with_location() -> None:
    """An ``asynchronous`` clock group serializes under ``clock_groups``
    (domain_map.py:125-126); a flop-sourced crossing serializes with a
    ``src_flop`` field (232-233); the destination flop's ``src``
    attribute yields a ``location`` (160); and passing the crossing in
    ``async_crossings`` flips ``async_per_sdc`` true via ``_crossing_keys``
    (309-310)."""
    spec = ClockSpec()
    spec.async_groups.append([{"ck0"}, {"ck1"}])

    src_cell = Cell(
        "f_src",
        "$dff",
        {"CLK": (1,), "D": (8,), "Q": (20,)},
        attributes={"src": "m.sv:5.1-5.9"},
    )
    dst_cell = Cell(
        "f_dst",
        "$dff",
        {"CLK": (2,), "D": (20,), "Q": (21,)},
        attributes={"src": "m.sv:9.1-9.9"},
    )
    src = Flop(cell=src_cell, clk=1, d=(8,), q=(20,))
    dst = Flop(cell=dst_cell, clk=2, d=(20,), q=(21,))
    crossing = Crossing(
        src_clock="ck0",
        dst_flop=dst,
        dst_clock="ck1",
        min_hops=0,
        width=1,
        src_flop=src,
    )
    fd_src = FlopDomain(flop=src, clock="ck0")
    fd_dst = FlopDomain(flop=dst, clock="ck1")
    module = _mod(ports={}, cells={"f_src": src_cell, "f_dst": dst_cell})
    dm = domain_map.build_domain_map(
        module,
        [fd_src, fd_dst],
        [crossing],
        spec,
        async_crossings=[crossing],
    )

    assert dm["clock_groups"] == [
        {"kind": "asynchronous", "members": [["ck0"], ["ck1"]]}
    ]
    cross = dm["crossings"]
    assert len(cross) == 1
    e = cross[0]
    assert e["src_flop"] == "m.f_src"
    assert e["dst_flop"] == "m.f_dst"
    assert e["async_per_sdc"] is True
    # Destination flop's src attribute surfaces as a location on its
    # flop-domains entry.
    flop_entries = {fe["instance_path"]: fe for fe in dm["flop_domains"]}
    assert "location" in flop_entries["m.f_dst"]


def test_crossing_keys_handles_port_source() -> None:
    """``_crossing_keys`` builds a stable key for a port-sourced crossing
    using ``src_port`` (domain_map.py:309-310 port branch). Passing it in
    ``async_crossings`` marks the matching serialized entry async."""
    dst_cell = Cell("f_dst", "$dff", {"CLK": (2,), "D": (1,), "Q": (3,)})
    dst = Flop(cell=dst_cell, clk=2, d=(1,), q=(3,))
    crossing = Crossing(
        src_clock="ck_in",
        dst_flop=dst,
        dst_clock="ck1",
        min_hops=1,
        width=1,
        src_port="din",
    )
    module = _mod(ports={}, cells={"f_dst": dst_cell})
    dm = domain_map.build_domain_map(
        module, [], [crossing], None, async_crossings=[crossing]
    )
    e = dm["crossings"][0]
    assert e["src_port"] == "din"
    assert e["async_per_sdc"] is True


def test_build_domain_map_pin_targeted_generated_clock_no_ports() -> None:
    """A pin-targeted generated clock (the common case) carries no
    ``ports`` list, so the serialized entry omits the key
    (domain_map.py:104->106 false branch)."""
    spec = ClockSpec()
    spec.clocks["gen"] = Clock(
        name="gen",
        period=8.0,
        ports=(),  # pin-targeted → no port list
        master="sys",
        is_generated=True,
    )
    module = _mod(ports={}, cells={})
    dm = domain_map.build_domain_map(module, [], [], spec)
    assert dm["generated_clocks"] == [{"name": "gen", "master": "sys", "period": 8.0}]


def test_build_domain_map_port_domains_filters_sentinel() -> None:
    """``port_domains`` lists SDC-typed ports but drops the
    unconstrained sentinel and any port not present in the module
    (domain_map.py:190 continue)."""
    spec = ClockSpec()
    spec.port_clock["data_in"] = "ck0"
    spec.port_clock["unc"] = UNCONSTRAINED_SENTINEL
    spec.port_clock["ghost"] = "ck1"  # not declared as a module port
    module = _mod(
        ports={"data_in": Port("data_in", "input", (5,))},
        cells={},
    )
    dm = domain_map.build_domain_map(module, [], [], spec)
    assert dm["port_domains"] == [
        {"module": "m", "port": "data_in", "clock": "ck0", "kind": "input"}
    ]


def test_build_domain_map_port_sourced_crossing_entry() -> None:
    """A port-sourced crossing serializes with a ``src_port`` field and no
    ``src_flop`` (domain_map.py:237)."""
    dst_cell = Cell("f_dst", "$dff", {"CLK": (2,), "D": (1,), "Q": (3,)})
    dst = Flop(cell=dst_cell, clk=2, d=(1,), q=(3,))
    crossing = Crossing(
        src_clock="ck_in",
        dst_flop=dst,
        dst_clock="ck1",
        min_hops=1,
        width=1,
        src_port="din",
    )
    module = _mod(ports={}, cells={"f_dst": dst_cell}, netnames={})
    dm = domain_map.build_domain_map(module, [], [crossing], None)
    entries = dm["crossings"]
    assert len(entries) == 1
    e = entries[0]
    assert e["src_port"] == "din"
    assert "src_flop" not in e
    assert e["src_clock"] == "ck_in"
    assert e["dst_clock"] == "ck1"
    # No SDC (None) → nothing is async_per_sdc.
    assert e["async_per_sdc"] is False


def test_build_domain_map_clock_network_crossings() -> None:
    """The additive ``clock_network_crossings`` list carries the gating
    cell metadata and is always ``async_per_sdc: true``
    (domain_map.py:268-)."""
    src_cell = Cell("f_src", "$dff", {"CLK": (1,), "Q": (10,)})
    dst_cell = Cell("f_dst", "$dff", {"CLK": (11,), "Q": (12,)})
    src = Flop(cell=src_cell, clk=1, d=(), q=(10,))
    dst = Flop(cell=dst_cell, clk=11, d=(), q=(12,))
    cnc = ClockNetworkCrossing(
        src_flop=src,
        src_clock="ck0",
        dst_flop=dst,
        dst_clock="ck1",
        control_cell="u_mux",
        control_cell_type="$mux",
        control_pin="S",
        control_kind="mux-select",
    )
    module = _mod(ports={}, cells={"f_src": src_cell, "f_dst": dst_cell})
    dm = domain_map.build_domain_map(
        module, [], [], None, clock_network_crossings=[cnc]
    )
    cn = dm["clock_network_crossings"]
    assert len(cn) == 1
    e = cn[0]
    assert e["control_cell"] == "u_mux"
    assert e["control_cell_type"] == "$mux"
    assert e["control_pin"] == "S"
    assert e["control_kind"] == "mux-select"
    assert e["src_clock"] == "ck0"
    assert e["dst_clock"] == "ck1"
    assert e["async_per_sdc"] is True


def test_serialize_flop_domains_source_instance_path() -> None:
    """A flop instantiated at top gets ``source_instance_path`` equal to
    the top module name and an empty parent chain (domain_map flop
    serialization)."""
    cell = Cell("f0", "$dff", {"CLK": (1,), "Q": (2,)})
    flop = Flop(cell=cell, clk=1, d=(), q=(2,))
    fd = FlopDomain(flop=flop, clock="ck0")
    module = _mod(ports={}, cells={"f0": cell})
    dm = domain_map.build_domain_map(module, [fd], [], None)
    entries = dm["flop_domains"]
    assert len(entries) == 1
    e = entries[0]
    assert e["clock"] == "ck0"
    assert e["instance_path"] == "m.f0"
    assert e["source_instance_path"] == "m"


def test_cell_leaf_name_flatten_and_slang_dotted() -> None:
    """``_cell_leaf_name`` strips Yosys ``$flatten\\`` prefixes back to the
    leaf and trims the slang dotted path to its trailing component
    (domain_map.py:371, 373)."""
    # slang dotted, non-$ prefix → trailing component (line 373).
    assert domain_map._cell_leaf_name("u_a.u_b.q") == "q"
    # flatten prefix with a $-leaf token → leaf captured.
    assert domain_map._cell_leaf_name("$flatten\\u_a.$procdff$7") == "$procdff$7"
    # flatten prefix whose body has no $-token → rsplit fallback (line 371).
    assert domain_map._cell_leaf_name("$flatten\\u_a.\\plain") == "plain"


def test_source_instance_path_none() -> None:
    """A ``None`` cell name yields ``None`` (domain_map.py:350)."""
    module = _mod(ports={}, cells={})
    assert domain_map._source_instance_path(module, None) is None


# --------------------------------------------------------------------------
# frontend — enum + dispatch
# --------------------------------------------------------------------------


def test_frontend_enum_str_values() -> None:
    """The enum members are stable string values the CLI surfaces."""
    assert Frontend.yosys.value == "yosys"
    assert Frontend.slang.value == "slang"
    assert Frontend.auto.value == "auto"


def test_elaborate_unknown_frontend_raises() -> None:
    """A non-enum frontend value is a programmer error and surfaces as a
    ValueError (frontend.py:88)."""
    with pytest.raises(ValueError, match="unknown frontend"):
        elaborate([Path("x.sv")], "top", frontend="bogus")  # type: ignore[arg-type]


def test_resolve_auto_probes_pyslang() -> None:
    """``resolve_auto`` returns a concrete frontend based on whether
    pyslang is importable (frontend.py:51-53). In the coverage CI job
    pyslang is present, so it resolves to slang."""
    from rtl_buddy_cdc.frontend import resolve_auto

    resolved = resolve_auto()
    assert resolved in (Frontend.slang, Frontend.yosys)
    if PYSLANG_INSTALLED:
        assert resolved is Frontend.slang


def test_elaborate_yosys_dispatch_passes_options(monkeypatch, tmp_path: Path):
    """The ``Frontend.yosys`` branch forwards sources, top, and the
    yosys-specific keyword options to the yosys frontend (frontend.py:74-83).
    We stub the concrete frontend so no real yosys binary runs."""
    seen: dict[str, object] = {}

    def _fake_elaborate(sources, top, *, yosys_bin, keep_json, plugin_path):
        seen.update(
            sources=sources,
            top=top,
            yosys_bin=yosys_bin,
            keep_json=keep_json,
            plugin_path=plugin_path,
        )
        return Module(name=top, ports={}, cells={}, netnames={})

    monkeypatch.setattr(yosys_fe, "elaborate", _fake_elaborate)
    src = tmp_path / "a.sv"
    keep = tmp_path / "k.json"
    module = elaborate(
        [src],
        "topmod",
        frontend=Frontend.yosys,
        yosys_bin="/opt/yosys",
        keep_json=keep,
        yosys_plugin="/opt/slang.so",
    )
    assert module.name == "topmod"
    assert seen["sources"] == [src]
    assert seen["top"] == "topmod"
    assert seen["yosys_bin"] == "/opt/yosys"
    assert seen["keep_json"] == keep
    # frontend.elaborate maps ``yosys_plugin`` -> ``plugin_path``.
    assert seen["plugin_path"] == "/opt/slang.so"


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_elaborate_auto_resolves_then_dispatches(tmp_path: Path, monkeypatch) -> None:
    """``Frontend.auto`` calls ``resolve_auto`` (frontend.py:73) and then
    dispatches to the resolved concrete frontend. We pin the resolution
    to slang and elaborate a trivial design through pyslang."""
    monkeypatch.setattr(frontend_mod, "resolve_auto", lambda: Frontend.slang)
    sv = tmp_path / "m.sv"
    sv.write_text(
        "module m(input logic clk, input logic d, output logic q);\n"
        "  always_ff @(posedge clk) q <= d;\n"
        "endmodule\n"
    )
    module = elaborate([sv], "m", frontend=Frontend.auto)
    assert module.name == "m"
    # The single always_ff lowers to a flop cell.
    assert any(is_ff_cell(c.type) for c in module.cells.values())


# --------------------------------------------------------------------------
# frontends.yosys — guard / error branches (no real yosys; monkeypatch)
# --------------------------------------------------------------------------


def test_yosys_missing_binary_raises(monkeypatch) -> None:
    """When yosys is not on PATH the frontend raises a YosysError with an
    actionable hint (yosys.py:51)."""
    monkeypatch.setattr(yosys_fe.shutil, "which", lambda name: None)
    with pytest.raises(YosysError, match="yosys not found on PATH"):
        yosys_fe.elaborate([Path("x.sv")], "top")


def test_yosys_explicit_bin_not_existing_raises(monkeypatch) -> None:
    """An explicit ``yosys_bin`` that doesn't exist on disk is rejected by
    the existence guard (yosys.py:50-51)."""
    # which() returning a path is irrelevant when yosys_bin is explicit;
    # the path simply doesn't exist.
    with pytest.raises(YosysError, match="yosys not found on PATH"):
        yosys_fe.elaborate([Path("x.sv")], "top", yosys_bin="/nonexistent/yosys-binary")


def test_yosys_plugin_missing_raises(monkeypatch, tmp_path: Path) -> None:
    """A configured plugin path that doesn't exist is rejected before any
    subprocess runs (yosys.py:54)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")
    with pytest.raises(YosysError, match="yosys plugin not found"):
        yosys_fe.elaborate(
            [Path("x.sv")],
            "top",
            yosys_bin=str(fake_yosys),
            plugin_path="/nonexistent/slang.so",
        )


def test_yosys_elaboration_failure_surfaces_streams(monkeypatch, tmp_path: Path):
    """A non-zero return code from the (fake) yosys subprocess is turned
    into a YosysError that surfaces both stderr and stdout
    (yosys.py:77-85)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")

    class _Proc:
        returncode = 1
        stderr = "ERROR: syntax error near token"
        stdout = "1. Executing Verilog frontend"

    def _fake_run(cmd, capture_output, text):  # noqa: ANN001
        # Sanity-check the dispatch wired through to the binary we passed.
        assert cmd[0] == str(fake_yosys)
        return _Proc()

    monkeypatch.setattr(yosys_fe.subprocess, "run", _fake_run)
    with pytest.raises(YosysError) as exc:
        yosys_fe.elaborate([Path("x.sv")], "top", yosys_bin=str(fake_yosys))
    msg = str(exc.value)
    assert "yosys elaboration failed" in msg
    assert "syntax error near token" in msg
    assert "Executing Verilog frontend" in msg


def test_yosys_failure_stderr_only(monkeypatch, tmp_path: Path) -> None:
    """When yosys fails with stderr but no stdout, the error message
    carries only the stderr text — the stdout-append branch is skipped
    (yosys.py:81->83)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")

    class _Proc:
        returncode = 1
        stderr = "ERROR: top module not found"
        stdout = "   "  # whitespace only → .strip() is falsy

    monkeypatch.setattr(yosys_fe.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(YosysError) as exc:
        yosys_fe.elaborate([Path("x.sv")], "top", yosys_bin=str(fake_yosys))
    msg = str(exc.value)
    assert "top module not found" in msg
    # No stdout body was appended.
    assert "\n" not in msg.split("top module not found", 1)[1]


def test_yosys_failure_empty_streams(monkeypatch, tmp_path: Path) -> None:
    """When yosys fails with both streams empty the error message is the
    bare headline — neither the stderr nor the stdout append branch fires
    (yosys.py:81->83 false branch)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")

    class _Proc:
        returncode = 2
        stderr = ""
        stdout = ""

    monkeypatch.setattr(yosys_fe.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(YosysError) as exc:
        yosys_fe.elaborate([Path("x.sv")], "top", yosys_bin=str(fake_yosys))
    assert str(exc.value) == "yosys elaboration failed"


def test_yosys_success_without_keep_json(monkeypatch, tmp_path: Path) -> None:
    """On success with ``keep_json=None`` the module loads and no copy is
    made (yosys.py:88->90 false branch)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")
    payload = json.dumps(
        {"modules": {"topmod": {"ports": {}, "cells": {}, "netnames": {}}}}
    )

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, capture_output, text):  # noqa: ANN001
        target = cmd[3].rsplit("write_json ", 1)[1].strip().strip("'")
        Path(target).write_text(payload)
        return _Proc()

    monkeypatch.setattr(yosys_fe.subprocess, "run", _fake_run)
    module = yosys_fe.elaborate([Path("a.sv")], "topmod", yosys_bin=str(fake_yosys))
    assert module.name == "topmod"


def test_yosys_temp_unlink_missing_is_swallowed(monkeypatch, tmp_path: Path):
    """If the intermediate JSON is already gone when the ``finally`` block
    runs, the ``FileNotFoundError`` on unlink is swallowed rather than
    masking the real outcome (yosys.py:94-95)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")

    class _Proc:
        returncode = 1
        stderr = "boom"
        stdout = ""

    def _fake_run(cmd, capture_output, text):  # noqa: ANN001
        # Delete the temp file the loader's finally-block expects, forcing
        # the FileNotFoundError branch.
        target = cmd[3].rsplit("write_json ", 1)[1].strip().strip("'")
        Path(target).unlink()
        return _Proc()

    monkeypatch.setattr(yosys_fe.subprocess, "run", _fake_run)
    # The YosysError still propagates; the missing temp file must not turn
    # into an unhandled FileNotFoundError.
    with pytest.raises(YosysError, match="boom"):
        yosys_fe.elaborate([Path("x.sv")], "top", yosys_bin=str(fake_yosys))


def test_yosys_plugin_path_builds_read_slang_command(monkeypatch, tmp_path: Path):
    """With a real (existing) plugin path the frontend builds a
    ``read_slang`` script instead of ``read_verilog`` (yosys.py:62). We
    intercept the subprocess to inspect the generated script without
    invoking a real yosys."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")
    fake_plugin = tmp_path / "slang.so"
    fake_plugin.write_text("")

    captured: dict[str, str] = {}

    class _Proc:
        returncode = 1  # fail fast after we capture the script
        stderr = "stop"
        stdout = ""

    def _fake_run(cmd, capture_output, text):  # noqa: ANN001
        # cmd == [yosys, "-q", "-p", script]
        captured["script"] = cmd[3]
        return _Proc()

    monkeypatch.setattr(yosys_fe.subprocess, "run", _fake_run)
    with pytest.raises(YosysError):
        yosys_fe.elaborate(
            [Path("a.sv")],
            "topmod",
            yosys_bin=str(fake_yosys),
            plugin_path=str(fake_plugin),
        )
    script = captured["script"]
    assert "read_slang" in script
    assert "read_verilog" not in script
    assert "--top topmod" in script
    assert str(fake_plugin) in script


def test_yosys_success_path_with_keep_json(monkeypatch, tmp_path: Path) -> None:
    """On a zero return code the frontend loads the JSON yosys "wrote" and
    honours ``keep_json`` by copying the intermediate file
    (yosys.py:87-90 success branch, exercised without a real yosys)."""
    fake_yosys = tmp_path / "yosys"
    fake_yosys.write_text("#!/bin/sh\n")
    keep = tmp_path / "kept.json"

    json_payload = json.dumps(
        {
            "modules": {
                "topmod": {
                    "ports": {"clk": {"direction": "input", "bits": [1]}},
                    "cells": {},
                    "netnames": {},
                }
            }
        }
    )

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, capture_output, text):  # noqa: ANN001
        # The script's final write_json target is the temp file path; the
        # fake yosys "writes" our payload there so netlist.load succeeds.
        script = cmd[3]
        target = script.rsplit("write_json ", 1)[1].strip().strip("'")
        Path(target).write_text(json_payload)
        return _Proc()

    monkeypatch.setattr(yosys_fe.subprocess, "run", _fake_run)
    module = yosys_fe.elaborate(
        [Path("a.sv")], "topmod", yosys_bin=str(fake_yosys), keep_json=keep
    )
    assert module.name == "topmod"
    assert module.ports["clk"].direction == "input"
    # keep_json copy was made.
    assert keep.exists()
    assert json.loads(keep.read_text())["modules"]["topmod"]


# --------------------------------------------------------------------------
# package entry point
# --------------------------------------------------------------------------


def test_package_main_invokes_app(monkeypatch) -> None:
    """``rtl_buddy_cdc.main`` is the console-script entry point: it calls
    the Typer ``app`` (``__init__.py`` body). We stub ``app`` so the call
    is observed without parsing the live argv, exercising the delegation
    line."""
    import rtl_buddy_cdc

    calls: list[bool] = []
    monkeypatch.setattr(rtl_buddy_cdc, "app", lambda: calls.append(True))
    rtl_buddy_cdc.main()
    assert calls == [True]
