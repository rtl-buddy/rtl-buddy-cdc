"""Per-bit port typing: the data shape and its readers (rtl-buddy-cdc#394).

``ClockSpec.port_clock`` keys a port typed bit by bit as ``d[3]`` — the
SDC's declared index — and every consumer reads it through
:class:`~rtl_buddy_cdc.sdc.PortTyping`. These tests pin the key
spelling against offset / descending ranges, the reader helpers, the
API path (``find_crossings`` given bit keys directly, no resolution),
and the sentinel stamping and re-resolution of a port's untyped bits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import format_bit_select
from rtl_buddy_cdc.sdc import (
    UNCONSTRAINED_SENTINEL,
    ClockSpec,
    PortDelay,
    port_bit_clocks,
    split_bit_key,
)

_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 5 [get_ports clk_b]
set_clock_groups -asynchronous -group clk_a -group clk_b
"""


def _module(tmp_path: Path) -> netlist.Module:
    """``d`` is ``[3:0]``, ``up`` is ``[0:3]`` (``upto``), ``off`` is
    ``[8:5]`` (offset 5); ``d`` and ``up`` feed one clk_b flop each."""
    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "clk_b": {"direction": "input", "bits": [3]},
        "d": {"direction": "input", "bits": [4, 5, 6, 7]},
        "up": {"direction": "input", "bits": [8, 9, 10, 11]},
        "off": {"direction": "input", "bits": [12, 13, 14, 15]},
        "q": {"direction": "output", "bits": [20, 21, 22, 23]},
        "uq": {"direction": "output", "bits": [24, 25, 26, 27]},
    }
    netnames = {
        name: {"bits": p["bits"], "hide_name": 0, "attributes": {}}
        for name, p in ports.items()
    }
    netnames["up"]["upto"] = 1
    netnames["off"]["offset"] = 5

    def dff(d: list[int], q: list[int]) -> dict:
        return {
            "type": "$dff",
            "parameters": {"CLK_POLARITY": "1", "WIDTH": str(len(d))},
            "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
            "connections": {"CLK": [3], "D": d, "Q": q},
        }

    raw = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": {
                    "r_d": dff([4, 5, 6, 7], [20, 21, 22, 23]),
                    "r_up": dff([8, 9, 10, 11], [24, 25, 26, 27]),
                },
                "netnames": netnames,
            }
        }
    }
    path = tmp_path / "top.json"
    path.write_text(json.dumps(raw))
    return netlist.load(path)


def test_declared_index_round_trip(tmp_path: Path) -> None:
    m = _module(tmp_path)
    assert [m.port_bit_index("d", p) for p in range(4)] == [0, 1, 2, 3]
    assert [m.port_bit_index("up", p) for p in range(4)] == [3, 2, 1, 0]
    assert [m.port_bit_index("off", p) for p in range(4)] == [5, 6, 7, 8]
    for name in ("d", "up", "off"):
        for p in range(4):
            assert m.port_bit_position(name, m.port_bit_index(name, p)) == p
    assert m.port_bit_position("off", 4) is None
    assert m.port_bit_position("off", 9) is None


def test_format_bit_select() -> None:
    assert format_bit_select([]) == ""
    assert format_bit_select([3]) == "[3]"
    assert format_bit_select([0, 1, 2]) == "[2:0]"
    assert format_bit_select([0, 1, 2, 4, 5, 6, 7]) == "[7:4,2:0]"
    assert format_bit_select([5, 1, 3]) == "[5,3,1]"


def test_split_bit_key() -> None:
    assert split_bit_key("d[3]") == ("d", 3)
    assert split_bit_key("a.b[-1]") == ("a.b", -1)
    assert split_bit_key("d") is None
    assert split_bit_key("d[*]") is None
    assert split_bit_key("d]") is None


def test_port_bit_clocks_reads_declared_indices(tmp_path: Path) -> None:
    m = _module(tmp_path)
    table = port_bit_clocks(
        {
            "d": "clk_a",
            "d[2]": "clk_b",
            "up[3]": "clk_b",
            "off[8]": "clk_a",
            "off[9]": "clk_a",  # no such bit: types nothing
            "nope[0]": "clk_a",  # no such port
            "zz*": "clk_a",  # an unresolved glob
        },
        m,
    )
    assert table == {
        "d": ("clk_a", "clk_a", "clk_b", "clk_a"),
        "up": ("clk_b", None, None, None),
        "off": (None, None, None, "clk_a"),
    }
    assert port_bit_clocks({"d": "clk_a", "q": "clk_b"}, m) == {}


def test_resolution_keys_bits_in_declared_indices(tmp_path: Path) -> None:
    m = _module(tmp_path)
    spec = sdc_mod.parse(
        _CLOCKS
        + "set_input_delay -clock clk_a 1 [get_ports {up[0] off[6]}]\n"
        + "set_input_delay -clock clk_b 1 [remove_from_collection "
        + "[get_ports off] [get_ports {off[6] off[5]}]]\n"
    )
    assert sdc_objects.resolve_port_delays(m, spec) == []
    assert spec.port_clock == {
        "up[0]": "clk_a",
        "off[6]": "clk_a",
        "off[7]": "clk_b",
        "off[8]": "clk_b",
    }


def test_port_typing_helpers(tmp_path: Path) -> None:
    m = _module(tmp_path)
    spec = ClockSpec(port_clock={"d[0]": "clk_a", "d[1]": "clk_a", "q": "clk_b"})
    spec.clocks["clk_b"] = sdc_mod.Clock("clk_b", 5.0, ("clk_b",))
    typing = spec.port_typing(m)
    assert typing.is_per_bit("d") and not typing.is_per_bit("q")
    assert typing.clock_of_bit("d", 1) == "clk_a"
    assert typing.clock_of_bit("d", 3) is None
    assert typing.clock_of_bit("d", 9) is None
    assert typing.clock_of_bit("q", 2) == "clk_b"
    assert typing.groups("q") == [("clk_b", None)]
    assert typing.groups("d") == [("clk_a", (0, 1)), (None, (2, 3))]
    assert typing.groups("d", [3, 0, 7]) == [("clk_a", (0,)), (None, (3,))]
    assert typing.label("d", (0, 1)) == "d[1:0]"
    assert typing.label("q", None) == "q"
    # Net-bit lookups: the bit's own typing, else a declared clock.
    assert typing.clock_of_net_bit(4) == "clk_a"
    assert typing.clock_of_net_bit(7) is None
    assert typing.clock_of_net_bit(3) == "clk_b"
    assert typing.clock_of_net_bit(99) is None
    assert spec.clock_for_port("d") is None


def test_find_crossings_reads_bit_keys_without_resolution(tmp_path: Path) -> None:
    """The API path: a hand-built ``port_clock`` with bit keys, passed
    straight to ``find_crossings``, walks only the typed bits."""
    m = _module(tmp_path)
    crossings = find_crossings(
        m,
        port_clock={"d[1]": "clk_a", "d[2]": "clk_a", "up[3]": "clk_a"},
        clock_for_port=lambda p: "clk_b" if p == "clk_b" else None,
    )
    got = sorted((c.src_port, c.src_port_bits, c.width) for c in crossings)
    assert got == [("d", (1, 2), 2), ("up", (3,), 1)]
    assert {c.src_name for c in crossings} == {"port d[2:1]", "port up[3]"}


def test_synthesize_stamps_only_the_untyped_bits(tmp_path: Path) -> None:
    m = _module(tmp_path)
    spec = sdc_mod.parse(_CLOCKS + "set_input_delay -clock clk_a 1 {up[0] up[1]}\n")
    stamped = sdc_mod.synthesize_unconstrained_inputs(spec, m)
    assert stamped == ["d", "up[3]", "up[2]", "off"]
    assert spec.port_clock["up[2]"] == UNCONSTRAINED_SENTINEL
    assert "up" not in spec.port_clock


def test_re_resolution_keeps_sentinels_on_untyped_bits_only(tmp_path: Path) -> None:
    """A delay added after ``synthesize_unconstrained_inputs`` is resolved
    on the next use; the sentinels it stamped fill only the bits no
    statement types."""
    m = _module(tmp_path)
    spec = sdc_mod.parse(_CLOCKS + "set_input_delay -clock clk_a 1 {d[0]}\n")
    sdc_mod.synthesize_unconstrained_inputs(spec, m)
    spec.port_delays.append(PortDelay("set_input_delay", "clk_b", ("d[1]",)))
    assert sdc_objects.resolve_port_delays(m, spec) == []
    assert {k: v for k, v in spec.port_clock.items() if k.startswith("d")} == {
        "d[0]": "clk_a",
        "d[1]": "clk_b",
        "d[2]": UNCONSTRAINED_SENTINEL,
        "d[3]": UNCONSTRAINED_SENTINEL,
    }


def test_hand_built_whole_key_fills_the_untyped_bits(tmp_path: Path) -> None:
    m = _module(tmp_path)
    spec = ClockSpec(
        port_clock={"d": "clk_b"},
        port_delays=[PortDelay("set_input_delay", "clk_a", ("d[2]",))],
    )
    assert sdc_objects.resolve_port_delays(m, spec) == []
    assert spec.port_clock == {
        "d[0]": "clk_b",
        "d[1]": "clk_b",
        "d[2]": "clk_a",
        "d[3]": "clk_b",
    }


@pytest.mark.parametrize("backend", ["tcl", "tokenizer"])
def test_partial_selection_agrees_across_backends(tmp_path: Path, backend: str) -> None:
    if backend == "tcl" and not sdc_mod.tcl_available():
        pytest.skip("_tkinter unavailable: no Tcl backend")
    m = _module(tmp_path)
    text = (
        _CLOCKS
        + "set_input_delay -clock clk_a 1 [get_ports d[1]]\n"
        + "set_input_delay -clock clk_b 1 [get_ports {d[2] d[3]}]\n"
        + "set_input_delay -clock clk_a 1 "
        + "[remove_from_collection [get_ports {up[*]}] [get_ports up[0]]]\n"
    )
    spec = sdc_mod.parse(text, backend=backend)
    sdc_objects.resolve_port_delays(m, spec)
    assert spec.port_clock == {
        "d[1]": "clk_a",
        "d[2]": "clk_b",
        "d[3]": "clk_b",
        "up[1]": "clk_a",
        "up[2]": "clk_a",
        "up[3]": "clk_a",
    }


# --- rules that walk back to port bits ----------------------------------------


def _rules_module(tmp_path: Path) -> netlist.Module:
    """``rst_n[0]`` resets a 2FF reset synchroniser in clk_a and two raw
    consumers in clk_b (RDC-008's shape); ``d[0] & d[1]`` feeds a clk_b
    2FF synchroniser through a gate (CDC-006's shape)."""
    one = "00000000000000000000000000000001"
    zero = "00000000000000000000000000000000"

    def adff(clk: int, d: int | str, q: int) -> dict:
        return {
            "type": "$adff",
            "parameters": {
                "ARST_POLARITY": zero,
                "ARST_VALUE": "0",
                "CLK_POLARITY": "1",
                "WIDTH": one,
            },
            "connections": {"ARST": [4], "CLK": [clk], "D": [d], "Q": [q]},
        }

    def dff(d: int, q: int) -> dict:
        return {
            "type": "$dff",
            "parameters": {"CLK_POLARITY": "1", "WIDTH": one},
            "connections": {"CLK": [3], "D": [d], "Q": [q]},
        }

    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "clk_b": {"direction": "input", "bits": [3]},
        "rst_n": {"direction": "input", "bits": [4, 5]},
        "d": {"direction": "input", "bits": [6, 7]},
        "q_a": {"direction": "output", "bits": [8]},
        "q_b": {"direction": "output", "bits": [9]},
        "s_q": {"direction": "output", "bits": [10]},
    }
    cells = {
        "m0": adff(2, "1", 14),
        "m1": adff(2, 14, 9),
        "c0": adff(3, 9, 13),
        "c1": adff(3, 13, 8),
        "g": {
            "type": "$and",
            "parameters": {"A_WIDTH": one, "B_WIDTH": one, "Y_WIDTH": one},
            "connections": {"A": [6], "B": [7], "Y": [11]},
        },
        "s0": dff(11, 12),
        "s1": dff(12, 10),
    }
    netnames = {
        name: {"bits": p["bits"], "hide_name": 0, "attributes": {}}
        for name, p in ports.items()
    }
    raw = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": netnames,
            }
        }
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(raw))
    return netlist.load(path)


def _fired(tmp_path: Path, extra: str) -> dict[str, list[str]]:
    from rtl_buddy_cdc.domain import filter_async
    from rtl_buddy_cdc.rules import run_all

    module = _rules_module(tmp_path)
    spec = sdc_mod.parse(_CLOCKS + extra)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = filter_async(
        find_crossings(module, port_clock=spec.port_clock, **spec.clock_root_inputs()),
        spec,
    )
    out: dict[str, list[str]] = {}
    for v in run_all(module, crossings, spec):
        out.setdefault(v.rule_id, []).append(v.message)
    return out


def test_cdc_006_judges_the_port_bits_it_reaches(tmp_path: Path) -> None:
    """``d[0]`` is typed to the synchroniser's own clock, ``d[1]`` to a
    foreign one: CDC-006 names ``d[1]`` alone."""
    fired = _fired(
        tmp_path,
        "set_input_delay -clock clk_b 1 {rst_n d[0]}\n"
        "set_input_delay -clock clk_a 1 {d[1]}\n",
    )
    (msg,) = fired["CDC-006"]
    assert "d[1] (clock=clk_a)" in msg and "d[0]" not in msg
    # Both bits on clk_b: same-domain timing asserted, nothing to report.
    assert "CDC-006" not in _fired(
        tmp_path, "set_input_delay -clock clk_b 1 {rst_n d}\n"
    )


def test_cdc_006_leaves_an_untyped_bit_to_cdc_011(tmp_path: Path) -> None:
    fired = _fired(tmp_path, "set_input_delay -clock clk_b 1 {rst_n d[0]}\n")
    assert "CDC-006" not in fired
    (msg,) = fired["CDC-011"]
    assert "'d[1]'" in msg


def test_rdc_008_checks_the_reset_bit_its_consumers_use(tmp_path: Path) -> None:
    """The clk_b consumers reset on ``rst_n[0]``. Typing that bit to clk_b
    says its deassertion is already synchronous there; typing only
    ``rst_n[1]`` says nothing about it, so RDC-008 fires — the whole-port
    reading used to stay silent."""
    assert "RDC-008" not in _fired(
        tmp_path, "set_input_delay -clock clk_b 1 {rst_n[0] d}\n"
    )
    fired = _fired(tmp_path, "set_input_delay -clock clk_b 1 {rst_n[1] d}\n")
    (msg,) = fired["RDC-008"]
    assert "'rst_n'" in msg and "clk_b" in msg
    assert "RDC-008" not in _fired(
        tmp_path, "set_input_delay -clock clk_b 1 {rst_n d}\n"
    )


def test_find_crossings_whole_key_with_bit_overrides(tmp_path: Path) -> None:
    """A whole-port key with a bit key over it (a hand-built spec): the
    port is walked per clock; an output bit key seeds nothing."""
    m = _module(tmp_path)
    crossings = find_crossings(
        m,
        port_clock={"d": "clk_a", "d[2]": "clk_b", "q[0]": "clk_a"},
        clock_for_port=lambda p: "clk_b" if p == "clk_b" else None,
    )
    assert [(c.src_port, c.src_port_bits) for c in crossings] == [("d", (0, 1, 3))]


def test_port_without_a_netname_uses_plain_indices(tmp_path: Path) -> None:
    """Hand-built netlists may carry no netname for a port: index = position."""
    raw = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": {"p": {"direction": "input", "bits": [2, 3, 4]}},
                "cells": {},
                "netnames": {},
            }
        }
    }
    path = tmp_path / "bare.json"
    path.write_text(json.dumps(raw))
    m = netlist.load(path)
    assert [m.port_bit_index("p", i) for i in range(3)] == [0, 1, 2]
    assert m.port_bit_position("p", 2) == 2
