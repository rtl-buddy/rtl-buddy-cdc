"""RDC-006 and a reset mux whose data legs are constants (rtl-buddy-cdc#354).

``rst_n ? 1'b0 : 1'b1`` lowers to a ``$mux`` with ``A = 1``, ``B = 0`` and
the reset on ``S``. Driving an async reset pin, that mux is an inverter of
``rst_n``: there is one reset source and nothing to select between, so
RDC-006 (muxed async reset) stays silent. A synthesis flow rewrites the
same mux into a ``$_NOT_``, which RDC-006 never looked at, so the two
netlists now agree. The controls keep a real second reset on a data leg
and still fire.

The netlists are built by hand so each mux shape is tested in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.rules import check_rdc_005, check_rdc_006

# Net bits: 2 clk, 3 rst_a, 4 rst_b, 5 sel, 6 d, 7 q, 8 reset net,
# 9 / 10 a second lane, 11 the AND of rst_a and rst_b.
_PORTS = {
    "clk": {"direction": "input", "bits": [2]},
    "rst_a": {"direction": "input", "bits": [3]},
    "rst_b": {"direction": "input", "bits": [4]},
    "sel": {"direction": "input", "bits": [5]},
    "d": {"direction": "input", "bits": [6]},
    "q": {"direction": "output", "bits": [7]},
}
_SDC = "create_clock -name clk -period 10 [get_ports clk]\n"


def _adff(arst: int) -> dict:
    return {
        "type": "$adff",
        "parameters": {
            "ARST_POLARITY": "0",
            "ARST_VALUE": "0",
            "CLK_POLARITY": "1",
            "WIDTH": "1",
        },
        "port_directions": {
            "ARST": "input",
            "CLK": "input",
            "D": "input",
            "Q": "output",
        },
        "connections": {"ARST": [arst], "CLK": [2], "D": [6], "Q": [7]},
    }


def _mux(cell_type: str, a: list, b: list, s: list, y: list) -> dict:
    params = {} if cell_type.startswith("$_") else {"WIDTH": str(len(y))}
    if cell_type == "$pmux":
        params["S_WIDTH"] = str(len(s))
    return {
        "type": cell_type,
        "parameters": params,
        "port_directions": {"A": "input", "B": "input", "S": "input", "Y": "output"},
        "connections": {"A": a, "B": b, "S": s, "Y": y},
    }


def _load(tmp_path: Path, cells: dict) -> netlist.Module:
    raw = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": _PORTS,
                "cells": cells,
                "netnames": {},
            }
        }
    }
    path = tmp_path / "top.json"
    path.write_text(json.dumps(raw))
    return netlist.load(path)


def _fired(module: netlist.Module, rule_id: str) -> int:
    spec = sdc_mod.parse(_SDC, backend="tokenizer")
    check = check_rdc_006 if rule_id == "RDC-006" else check_rdc_005
    return sum(v.rule_id == rule_id for v in check(module, [], spec))


@pytest.mark.parametrize("cell_type", ["$mux", "$_MUX_"])
@pytest.mark.parametrize(("a", "b"), [("1", "0"), ("0", "1")], ids=["inv", "buf"])
def test_constant_leg_mux_on_reset_is_not_a_reset_mux(
    tmp_path: Path, cell_type: str, a: str, b: str
) -> None:
    module = _load(
        tmp_path,
        {"ff": _adff(8), "m": _mux(cell_type, [a], [b], [3], [8])},
    )
    assert _fired(module, "RDC-006") == 0


@pytest.mark.parametrize("cell_type", ["$mux", "$_MUX_"])
def test_two_reset_legs_still_fire(tmp_path: Path, cell_type: str) -> None:
    """Control: both data legs are reset ports, a real reset mux."""
    module = _load(
        tmp_path,
        {"ff": _adff(8), "m": _mux(cell_type, [3], [4], [5], [8])},
    )
    assert _fired(module, "RDC-006") == 1


@pytest.mark.parametrize("cell_type", ["$mux", "$_MUX_"])
def test_one_constant_leg_and_one_reset_leg_still_fires(
    tmp_path: Path, cell_type: str
) -> None:
    """Control: ``rst_a ? rst_b : 1`` gates a second reset; only an
    all-constant mux is exempt."""
    module = _load(
        tmp_path,
        {"ff": _adff(8), "m": _mux(cell_type, ["1"], [4], [3], [8])},
    )
    assert _fired(module, "RDC-006") == 1


@pytest.mark.parametrize(("lane", "expected"), [(0, 0), (1, 1)])
def test_multibit_mux_is_judged_per_lane(
    tmp_path: Path, lane: int, expected: int
) -> None:
    """A 2-bit ``$mux`` whose lane 0 is constant on both legs and whose
    lane 1 selects between the two resets: only the lane the flop's
    reset reads decides."""
    y = [8, 9]
    module = _load(
        tmp_path,
        {
            "ff": _adff(y[lane]),
            "m": _mux("$mux", ["1", 3], ["0", 4], [5], y),
        },
    )
    assert _fired(module, "RDC-006") == expected


def test_all_constant_pmux_is_not_a_reset_mux(tmp_path: Path) -> None:
    module = _load(
        tmp_path,
        {"ff": _adff(8), "m": _mux("$pmux", ["1"], ["0", "0"], [3, 4], [8])},
    )
    assert _fired(module, "RDC-006") == 0


def test_constant_leg_mux_gets_no_rdc_005_exemption(tmp_path: Path) -> None:
    """RDC-005 exempts a mux on the reset pin as explicit source
    selection. A constant-leg mux selects nothing, so two resets ANDed
    into its select converge exactly as they would through the
    ``$_NOT_`` a synthesis flow makes of it."""
    and_cell = {
        "type": "$and",
        "parameters": {"A_WIDTH": "1", "B_WIDTH": "1", "Y_WIDTH": "1"},
        "port_directions": {"A": "input", "B": "input", "Y": "output"},
        "connections": {"A": [3], "B": [4], "Y": [11]},
    }
    module = _load(
        tmp_path,
        {"ff": _adff(8), "g": and_cell, "m": _mux("$mux", ["1"], ["0"], [11], [8])},
    )
    assert _fired(module, "RDC-005") == 1
    assert _fired(module, "RDC-006") == 0
