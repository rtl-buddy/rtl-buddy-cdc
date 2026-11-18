"""Element selection on multi-dimensional names (rtl-buddy-cdc#402 item 7).

What the netlist keeps, on both frontends:

- a packed multi-dimensional vector (``logic [1:0][3:0] pk``) is ONE
  netname of 8 bits with no record of its dimensions, so ``pk[1][2]``
  has no dimension to select from. It selects nothing, and the
  "matches nothing" warning now says why and what to write instead (the
  flat index) rather than suggesting a typo. ``pk[1]`` keeps its
  documented reading: bit 1 by declared index.
- a multi-dimensional unpacked array (``logic [3:0] grid [0:1][0:2]``)
  is one netname per element, named by row-major position
  (``grid[0]`` .. ``grid[5]``), with nothing recording the shape. So
  ``grid[1][2]`` reads as bit 2 of element 1, and the analyzer cannot
  tell that apart from element (1, 2): that reading is kept, and
  documented.

The netlist tests are hand-written in the shape both frontends emit; the
slang check pins that shape and skips without pyslang.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import case_analysis_warnings

_SV = """\
module grids (input clk_a, input clk_b, input [7:0] din, output [7:0] dq,
              output [3:0] gq);
    logic [1:0][3:0] pk, pk_q;
    logic [3:0] grid [0:1][0:2];
    logic [3:0] g_q;
    always_ff @(posedge clk_a) begin
        pk <= din;
        for (int r = 0; r < 2; r++)
            for (int c = 0; c < 3; c++)
                grid[r][c] <= din[3:0] + 4'(r * 3 + c);
    end
    always_ff @(posedge clk_b) begin
        pk_q <= pk;
        g_q <= grid[1][2];
    end
    assign dq = pk_q;
    assign gq = g_q;
endmodule
"""


@pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)
def test_slang_netlist_keeps_no_dimensions(tmp_path: Path) -> None:
    """The slang frontend names the shapes as Yosys does (read_verilog -sv
    + proc, and read_slang, give the same names): one flat ``pk`` and
    row-major ``grid[0..5]``, with no dimension record either way."""
    from rtl_buddy_cdc.frontends import slang

    src = tmp_path / "grids.sv"
    src.write_text(_SV)
    module = slang.elaborate([src], "grids")
    pk = module.netnames["pk"]
    assert (len(pk.bits), pk.offset, pk.upto) == (8, 0, False)
    assert set(pk.attributes) <= {"src"}
    elements = sorted(n for n in module.netnames if n.startswith("grid["))
    assert elements == [f"grid[{i}]" for i in range(6)]


def _dff(clk: int, d: list[int], q: list[int]) -> dict:
    return {
        "type": "$dff",
        "parameters": {"CLK_POLARITY": "1", "WIDTH": str(len(d))},
        "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
        "connections": {"CLK": [clk], "D": d, "Q": q},
    }


def _net(bits: list[int]) -> dict:
    return {"bits": bits, "hide_name": 0, "attributes": {}}


def _grids(tmp_path: Path) -> Path:
    """The netlist both frontends emit for ``_SV`` (two of the six grid
    elements, which is all the selection needs)."""
    din = list(range(10, 18))
    pk = list(range(20, 28))
    raw = {
        "modules": {
            "grids": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk_a": {"direction": "input", "bits": [2]},
                    "clk_b": {"direction": "input", "bits": [3]},
                    "din": {"direction": "input", "bits": din},
                    "dq": {"direction": "output", "bits": list(range(30, 38))},
                    "gq": {"direction": "output", "bits": list(range(50, 54))},
                },
                "cells": {
                    "$procdff$1": _dff(2, din, pk),
                    "$procdff$2": _dff(3, pk, list(range(30, 38))),
                    "$procdff$3": _dff(2, din[:4], [40, 41, 42, 43]),
                    "$procdff$4": _dff(2, din[4:], [44, 45, 46, 47]),
                    "$procdff$5": _dff(3, [44, 45, 46, 47], list(range(50, 54))),
                },
                "netnames": {
                    "clk_a": _net([2]),
                    "clk_b": _net([3]),
                    "din": _net(din),
                    "dq": _net(list(range(30, 38))),
                    "gq": _net(list(range(50, 54))),
                    "pk": _net(pk),
                    "pk_q": _net(list(range(30, 38))),
                    "grid[1]": _net([40, 41, 42, 43]),
                    "grid[5]": _net([44, 45, 46, 47]),
                },
            }
        }
    }
    path = tmp_path / "grids.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.mark.parametrize(
    ("kind", "pattern", "want"),
    [
        ("nets", "pk[1][2]", set()),
        ("registers", "pk_reg[1][2]", {}),
        ("registers", "pk_reg[1][*]", {}),
        # One index is the declared (flat) bit, as on any vector.
        ("nets", "pk[6]", {26}),
        ("registers", "pk_reg[1]", {"$procdff$1": {1}}),
        # Row-major element 1, bit 2 — not element (1, 2), which is grid[5].
        ("nets", "grid[1][2]", {42}),
        ("registers", "grid_reg[5][0]", {"$procdff$4": {0}}),
    ],
)
def test_selection(tmp_path: Path, kind: str, pattern: str, want: set | dict) -> None:
    index = sdc_objects.NameIndex(netlist.load(_grids(tmp_path)))
    got = index.net_bits(pattern) if kind == "nets" else index.registers(pattern)
    assert got == want


_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1 [get_ports din]
set_output_delay -clock clk_b 1 [get_ports {dq gq}]
"""

_ADVICE = "is a single vector in the netlist"


def _warnings(tmp_path: Path, extra: str) -> list[str]:
    sdc = tmp_path / "grids.sdc"
    sdc.write_text(_CLOCKS + extra + "\n")
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(_grids(tmp_path)), "-s", str(sdc)]
    )
    assert result.exit_code in (0, 1), result.output
    return [ln for ln in result.output.splitlines() if ln.startswith("warning:")]


@pytest.mark.parametrize(
    ("statement", "name"),
    [
        ("set_false_path -from [get_cells {pk_reg[1][2]}]", "pk_reg"),
        ("set_false_path -from [get_pins {pk_reg[1][*]/Q}]", "pk_reg"),
        ("set_false_path -through [get_nets {pk[0][3]}]", "pk"),
        ("set_max_delay -datapath_only 2 -from [get_cells {pk_reg[0][1]}]"
         " -to [get_cells pk_q_reg]", "pk_reg"),
        ("set_input_delay -clock clk_a 1 [get_ports {din[1][2]}]", "din"),
    ],
    ids=["cells", "pins-glob", "through", "datapath-only", "input-delay"],
)  # fmt: skip
def test_packed_double_index_warns_with_the_flat_index(
    tmp_path: Path, sdc_backend: str, statement: str, name: str
) -> None:
    """``x[i][j]`` on a single vector names nothing; the warning says why
    and points at ``x[n]``, on either SDC backend."""
    (warning,) = _warnings(tmp_path, statement)
    assert f"'{name}' {_ADVICE}" in warning
    assert f"'{name}[n]' with n = i*W + j" in warning
    assert "constraint typo" not in warning


@pytest.mark.parametrize(
    "statement",
    [
        "set_false_path -from [get_cells {nope_reg[1][2]}]",  # no such name
        "set_false_path -from [get_cells {grid_reg[1][7]}]",  # element exists
        "set_false_path -from [get_cells {pk_reg[9]}]",  # one index
    ],
    ids=["typo", "element-out-of-range", "single-index"],
)
def test_other_misses_stay_typos(
    tmp_path: Path, sdc_backend: str, statement: str
) -> None:
    (warning,) = _warnings(tmp_path, statement)
    assert "constraint typo?" in warning
    assert _ADVICE not in warning


def test_case_analysis_on_a_double_indexed_port(tmp_path: Path) -> None:
    module = netlist.load(_grids(tmp_path))
    (warning,) = case_analysis_warnings(module, {"din[1][0]": "0"})
    assert f"'din' {_ADVICE}" in warning
    (plain,) = case_analysis_warnings(module, {"nope[1][0]": "0"})
    assert plain.endswith("no such top-level port — ignored for clock tracing")
