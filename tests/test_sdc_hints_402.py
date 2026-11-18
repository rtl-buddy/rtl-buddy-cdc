"""Every SDC command a finding suggests is SDC (rtl-buddy-cdc#402 item 6).

A fix hint is written as one complete command in backticks — numbers the
analyzer cannot know (an external delay, a clock period) carry an
explicit placeholder value the message says to replace — so it can be
pasted as is. Each test takes the hint out of a real finding, parses it
verbatim on both SDC backends, and checks that adding it to the SDC does
what it says and clears the finding.

The netlists are written by hand (no yosys needed).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.rules import SDC_HINT_DELAY, SDC_HINT_PERIOD, _tcl_word
from rtl_buddy_cdc.sdc import parse

_HINT = re.compile(r"`((?:set_[a-z_]+|create_[a-z_]+) [^`]*)`")


def _dff(clk: int, d: list[int], q: list[int]) -> dict:
    return {
        "type": "$dff",
        "parameters": {"CLK_POLARITY": "1", "WIDTH": str(len(d))},
        "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
        "connections": {"CLK": [clk], "D": d, "Q": q},
    }


def _netlist(tmp_path: Path, ports: dict, cells: dict) -> Path:
    netnames = {
        name: {"bits": spec["bits"], "hide_name": 0, "attributes": {}}
        for name, spec in ports.items()
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
    path = tmp_path / "top.json"
    path.write_text(json.dumps(raw))
    return path


def _analyze(path: Path, sdc_text: str, tmp_path: Path) -> list[dict]:
    sdc = tmp_path / "top.sdc"
    sdc.write_text(sdc_text)
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(path), "-s", str(sdc), "-f", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.stdout)["violations"]


_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
"""

_BASE_PORTS = {
    "clk_a": {"direction": "input", "bits": [2]},
    "clk_b": {"direction": "input", "bits": [3]},
}


def _hint_of(violations: list[dict], rule: str) -> str:
    (finding,) = [v for v in violations if v["rule_id"] == rule]
    (hint,) = _HINT.findall(finding["message"])
    assert "placeholder" in finding["message"]
    return hint


def _round_trip(path: Path, sdc_text: str, rule: str, tmp_path: Path) -> str:
    """The hint ``rule``'s finding carries, after checking that it parses
    clean and that adding it clears the finding."""
    hint = _hint_of(_analyze(path, sdc_text, tmp_path), rule)
    assert not parse(sdc_text + hint + "\n").partial_warnings
    fixed = _analyze(path, sdc_text + hint + "\n", tmp_path)
    assert rule not in {v["rule_id"] for v in fixed}
    return hint


def test_cdc_011_hint_types_the_bits_it_names(tmp_path: Path, sdc_backend: str) -> None:
    ports = {
        **_BASE_PORTS,
        "din": {"direction": "input", "bits": [10, 11]},
        "q": {"direction": "output", "bits": [12, 13]},
    }
    path = _netlist(tmp_path, ports, {"r": _dff(3, [10, 11], [12, 13])})
    sdc = _CLOCKS + "set_output_delay -clock clk_b 1 [get_ports q]\n"
    hint = _round_trip(path, sdc, "CDC-011", tmp_path)
    assert hint == f"set_input_delay -clock clk_b {SDC_HINT_DELAY} [get_ports din]"
    spec = parse(sdc + hint + "\n")
    (delay,) = [d for d in spec.port_delays if d.command == "set_input_delay"]
    assert (delay.clock, delay.targets) == ("clk_b", ("din",))


def test_cdc_011_two_domain_hint_is_one_clock(tmp_path: Path, sdc_backend: str) -> None:
    """Captured in two domains: the hint types the port with one of them,
    an example the message says to replace with the true source clock."""
    ports = {
        **_BASE_PORTS,
        "din": {"direction": "input", "bits": [10]},
        "qa": {"direction": "output", "bits": [12]},
        "qb": {"direction": "output", "bits": [13]},
    }
    cells = {"ra": _dff(2, [10], [12]), "rb": _dff(3, [10], [13])}
    path = _netlist(tmp_path, ports, cells)
    sdc = _CLOCKS + (
        "set_output_delay -clock clk_a 1 [get_ports qa]\n"
        "set_output_delay -clock clk_b 1 [get_ports qb]\n"
    )
    hint = _round_trip(path, sdc, "CDC-011", tmp_path)
    assert hint == f"set_input_delay -clock clk_a {SDC_HINT_DELAY} [get_ports din]"


def test_cdc_011_bus_bit_hint_is_braced(tmp_path: Path, sdc_backend: str) -> None:
    """One bit of a bus is typed: the hint names the other with braces,
    or the Tcl reader would run ``[1]`` as a command."""
    ports = {
        **_BASE_PORTS,
        "din": {"direction": "input", "bits": [10, 11]},
        "q": {"direction": "output", "bits": [12, 13]},
    }
    path = _netlist(tmp_path, ports, {"r": _dff(3, [10, 11], [12, 13])})
    sdc = _CLOCKS + (
        "set_output_delay -clock clk_b 1 [get_ports q]\n"
        "set_input_delay -clock clk_b 1 [get_ports {din[0]}]\n"
    )
    hint = _round_trip(path, sdc, "CDC-011", tmp_path)
    assert (
        hint == f"set_input_delay -clock clk_b {SDC_HINT_DELAY} [get_ports {{din[1]}}]"
    )
    module = netlist.load(path)
    spec = parse(sdc + hint + "\n")
    sdc_objects.resolve_port_delays(module, spec)
    typing = spec.port_typing(module)
    assert [typing.clock_of_bit("din", i) for i in (0, 1)] == ["clk_b", "clk_b"]


def test_cdc_021_hint_declares_the_clock(tmp_path: Path, sdc_backend: str) -> None:
    ports = {
        **_BASE_PORTS,
        "ck": {"direction": "input", "bits": [4]},
        "din": {"direction": "input", "bits": [10]},
        "q": {"direction": "output", "bits": [12]},
    }
    path = _netlist(tmp_path, ports, {"r": _dff(4, [10], [12])})
    sdc = _CLOCKS + "set_input_delay -clock clk_a 1 [get_ports din]\n"
    hint = _round_trip(path, sdc, "CDC-021", tmp_path)
    assert hint == f"create_clock -name ck -period {SDC_HINT_PERIOD} [get_ports ck]"
    clock = parse(sdc + hint + "\n").clocks["ck"]
    assert (clock.period, clock.ports) == (float(SDC_HINT_PERIOD), ("ck",))


def test_cdc_025_hint_contracts_the_output(tmp_path: Path, sdc_backend: str) -> None:
    ports = {
        **_BASE_PORTS,
        "din": {"direction": "input", "bits": [10, 11]},
        "qa": {"direction": "output", "bits": [12]},
        "qb": {"direction": "output", "bits": [13]},
    }
    cells = {"ra": _dff(2, [10], [12]), "rb": _dff(3, [11], [13])}
    path = _netlist(tmp_path, ports, cells)
    sdc = _CLOCKS + (
        "set_input_delay -clock clk_a 1 [get_ports {din[0]}]\n"
        "set_input_delay -clock clk_b 1 [get_ports {din[1]}]\n"
        "set_output_delay -clock clk_a 1 [get_ports qa]\n"
    )
    hint = _round_trip(path, sdc, "CDC-025", tmp_path)
    assert hint == f"set_output_delay -clock clk_b {SDC_HINT_DELAY} [get_ports qb]"


@pytest.mark.parametrize(
    ("name", "word"),
    [("clk_a", "clk_a"), ("a.b/c-1:2", "a.b/c-1:2"), ("v[0]", "{v[0]}"),
     ("x$y", "{x$y}"), ("p q", "{p q}")],
)  # fmt: skip
def test_tcl_word_braces_only_what_tcl_would_substitute(name: str, word: str) -> None:
    assert _tcl_word(name) == word
